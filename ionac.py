#!/usr/bin/env python3
"""ionac - a compiler for the Iona programming language.

Iona is an experimental systems language:
  - Procedural, with block structure defined by indentation (like Python).
  - Within a statement, the syntax is postfix (like Forth): operands precede
    the operator, so `3 4 +` means 3 + 4 and `N FACTORIAL` calls FACTORIAL(N).
  - Compiles directly to machine code (this v0 emits C and hands it to `cc`).
  - No garbage collection: locals are plain machine words.
  - Source is UPPERCASE with terse operators, staying within a 1960s teletype's
    character set.

It is statically typed. Every parameter, local, and return type is written
explicitly (no defaults), the type follows the name, and there are *no implicit
conversions* -- crossing between byte/word/float happens only through explicit
conversion functions. See README.md for the language reference.

This file is the whole toolchain: tokenizer, an indentation-based block parser,
and a code generator that lowers postfix statements to C by maintaining a
*compile-time* operand stack of typed C expressions.
"""

import sys
import os
import re
import subprocess


class IonaError(Exception):
    """A user-facing compile error, tagged with a source line number."""

    def __init__(self, lineno, msg):
        super().__init__(msg)
        self.lineno = lineno
        self.msg = msg


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

# Surface operators. `=` is equality and `<>` is not-equal (ALGOL-style). `!` is
# assignment when it trails a name and the definition marker when it leads a
# line. `?`/`&` trail a condition (conditional / while loop), `@` trails a value
# to return it, and `$` prefixes a type to mean "pointer to". These markers are
# lexed as `op` tokens; the parser interprets them by position.
# Multi-character operators must be tried before their single-char prefixes.
OPERATORS = ["<=", ">=", "<>", "<", ">", "=", "!", "?", "@", "&", "$",
             "+", "-", "*", "/", "%"]
COMPARES = {"<=", ">=", "<>", "<", ">", "="}

# C spellings for the operators whose Iona form differs.
C_OP = {"=": "==", "<>": "!="}

# Short-circuit logical connectives: condition-only control-flow words.
LOGICAL = {"AND", "OR", "NOT"}

# Identifiers are UPPERCASE letters and digits only (a 1960s teletype had no
# lowercase, and its 0x5F position was a left-arrow rather than `_`).
_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*")
_FLOAT_RE = re.compile(r"[0-9]+\.[0-9]+")
_INT_RE = re.compile(r"[0-9]+")


class Token:
    __slots__ = ("kind", "value", "lineno")

    def __init__(self, kind, value, lineno):
        self.kind = kind  # 'int' | 'float' | 'str' | 'op' | 'name' | 'comma'
        self.value = value
        self.lineno = lineno

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r})"


def tokenize_line(text, lineno):
    """Tokenize one physical line (a `;` begins a comment to end of line)."""
    toks = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t":
            i += 1
            continue
        if c == ";":
            break  # rest of line is a comment
        if c == ",":
            toks.append(Token("comma", ",", lineno))
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise IonaError(lineno, "unterminated string literal")
            toks.append(Token("str", "".join(buf), lineno))
            i = j + 1
            continue
        m = _FLOAT_RE.match(text, i)        # try float before int: `3.14`
        if m:
            toks.append(Token("float", m.group(), lineno))
            i = m.end()
            continue
        m = _INT_RE.match(text, i)
        if m:
            toks.append(Token("int", m.group(), lineno))
            i = m.end()
            continue
        m = _NAME_RE.match(text, i)
        if m:
            toks.append(Token("name", m.group(), lineno))
            i = m.end()
            continue
        for op in OPERATORS:
            if text.startswith(op, i):
                toks.append(Token("op", op, lineno))
                i += len(op)
                break
        else:
            raise IonaError(lineno, f"unexpected character {c!r}")
    return toks


# --------------------------------------------------------------------------
# Logical-line reader (indentation)
# --------------------------------------------------------------------------

class Line:
    __slots__ = ("indent", "tokens", "lineno")

    def __init__(self, indent, tokens, lineno):
        self.indent = indent
        self.tokens = tokens
        self.lineno = lineno


def read_lines(src):
    """Split source into non-blank logical lines with their indent levels."""
    lines = []
    for lineno, raw in enumerate(src.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise IonaError(lineno, "tabs are not allowed for indentation; use spaces")
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        toks = tokenize_line(raw[indent:], lineno)
        if not toks:
            continue  # blank or comment-only line
        lines.append(Line(indent, toks, lineno))
    return lines


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------
# Scalars V/B/W/F map to void/char/size_t/double; `$T` is a pointer. Records and
# arrays are recognized by the grammar but not yet implemented (they arrive with
# the field/index access operators).

class Scalar:
    __slots__ = ("k",)

    def __init__(self, k):
        self.k = k                      # 'V' | 'B' | 'W' | 'F'

    def __eq__(self, o):
        return isinstance(o, Scalar) and o.k == self.k

    def __hash__(self):
        return hash(("S", self.k))

    def iname(self):
        return self.k


class Ptr:
    __slots__ = ("e",)

    def __init__(self, e):
        self.e = e                      # element Type

    def __eq__(self, o):
        return isinstance(o, Ptr) and o.e == self.e

    def __hash__(self):
        return hash(("P", self.e))

    def iname(self):
        return "$" + self.e.iname()


VOID = Scalar("V")
BYTE = Scalar("B")
WORD = Scalar("W")
FLOAT = Scalar("F")
SCALARS = {"V": VOID, "B": BYTE, "W": WORD, "F": FLOAT}

# Explicit conversion functions: postfix, one operand. (source, target, C cast)
CONVERSIONS = {
    "B2W": (BYTE, WORD, "size_t"),
    "W2B": (WORD, BYTE, "char"),
    "W2F": (WORD, FLOAT, "double"),
    "F2W": (FLOAT, WORD, "size_t"),
    "B2F": (BYTE, FLOAT, "double"),
    "F2B": (FLOAT, BYTE, "char"),
}


def ctype(t):
    """The C type for an Iona type."""
    if isinstance(t, Scalar):
        return {"V": "void", "B": "char", "W": "size_t", "F": "double"}[t.k]
    if isinstance(t, Ptr):
        return ctype(t.e) + "*"
    raise IonaError(0, "internal: no C type for this type")


def zero_init(t):
    """A C zero-initializer for a declaration of this type."""
    return "0"


def parse_type_expr(toks, lineno):
    """Parse a prefix type expression that must occupy all of `toks`."""
    ty, i = _parse_type(toks, 0, lineno)
    if i != len(toks):
        raise IonaError(lineno, "trailing tokens after a type")
    return ty


def _parse_type(toks, i, lineno):
    if i >= len(toks):
        raise IonaError(lineno, "expected a type")
    t = toks[i]
    if t.kind == "op" and t.value == "$":
        elem, j = _parse_type(toks, i + 1, lineno)
        return Ptr(elem), j
    if t.kind == "name":
        v = t.value
        if v in SCALARS:
            return SCALARS[v], i + 1
        if v == "A":
            raise IonaError(lineno, "array types are not implemented yet (coming with indexing)")
        if v == "R":
            raise IonaError(lineno, "record types are not implemented yet (coming with field access)")
        raise IonaError(lineno, f"unknown type `{v}`")
    raise IonaError(lineno, f"expected a type, found `{t.value}`")


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------

class FuncDef:
    def __init__(self, name, ret, params, body, lineno):
        self.name = name
        self.ret = ret                  # Type
        self.params = params            # list of (name, Type)
        self.body = body
        self.lineno = lineno


class Decl:
    def __init__(self, name, type, lineno):
        self.name = name
        self.type = type
        self.lineno = lineno


class If:
    def __init__(self, cond, then_body, else_body, lineno):
        self.cond = cond                # list[Token]
        self.then_body = then_body
        self.else_body = else_body
        self.lineno = lineno


class While:
    def __init__(self, cond, body, lineno):
        self.cond = cond
        self.body = body
        self.lineno = lineno


class Stmt:
    def __init__(self, tokens, lineno):
        self.tokens = tokens
        self.lineno = lineno


# --------------------------------------------------------------------------
# Block parser
# --------------------------------------------------------------------------

def is_def(tokens):
    """A `!`-led line: a top-level definition or a local declaration."""
    return tokens[0].kind == "op" and tokens[0].value == "!"


def is_else(tokens):
    """The else branch: a line that is just `/` (never a valid statement)."""
    return len(tokens) == 1 and tokens[0].kind == "op" and tokens[0].value == "/"


def is_header(tokens):
    """True if the line opens an indented block (`!` def, `?`/`&` trailing, `/`)."""
    if is_def(tokens):
        return True
    last = tokens[-1]
    if last.kind == "op" and last.value in ("?", "&"):
        return True
    return is_else(tokens)


def split_groups(toks, lineno):
    """Split tokens on commas into non-empty groups."""
    groups, cur = [], []
    for t in toks:
        if t.kind == "comma":
            groups.append(cur)
            cur = []
        else:
            cur.append(t)
    groups.append(cur)
    for g in groups:
        if not g:
            raise IonaError(lineno, "empty group around a comma")
    return groups


class Parser:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def parse_module(self):
        defs = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.indent != 0:
                raise IonaError(line.lineno, "unexpected indentation at top level")
            if not is_def(line.tokens):
                raise IonaError(line.lineno, "only `!NAME ...` declarations are allowed at top level")
            defs.append(self.parse_def())
        return defs

    def parse_def(self):
        line = self.lines[self.i]
        body_toks = line.tokens[1:]     # after the `!`
        if not body_toks:
            raise IonaError(line.lineno, "malformed declaration")
        if body_toks[0].kind == "name" and body_toks[0].value == "R":
            raise IonaError(line.lineno, "record types are not implemented yet (coming with field access)")
        groups = split_groups(body_toks, line.lineno)
        head = groups[0]
        fname = head[0]
        if fname.kind != "name":
            raise IonaError(line.lineno, "expected a function name")
        if fname.value in LOGICAL:
            raise IonaError(line.lineno, f"`{fname.value}` is reserved and cannot be a name")
        ret = parse_type_expr(head[1:], line.lineno)   # no default: required
        params = []
        for g in groups[1:]:
            pname = g[0]
            if pname.kind != "name":
                raise IonaError(line.lineno, "expected a parameter name")
            if pname.value in LOGICAL:
                raise IonaError(line.lineno, f"`{pname.value}` is reserved and cannot be a name")
            params.append((pname.value, parse_type_expr(g[1:], line.lineno)))
        self.i += 1
        body = self.parse_block(line.indent)
        return FuncDef(fname.value, ret, params, body, line.lineno)

    def parse_decl(self):
        """A local declaration inside a body: `!NAME TYPE`."""
        line = self.lines[self.i]
        body_toks = line.tokens[1:]
        if body_toks and body_toks[0].kind == "name" and body_toks[0].value == "R":
            raise IonaError(line.lineno, "records can only be defined at top level")
        if len(body_toks) < 2 or body_toks[0].kind != "name":
            raise IonaError(line.lineno, "malformed local declaration: expected `!NAME TYPE`")
        name = body_toks[0].value
        if name in LOGICAL:
            raise IonaError(line.lineno, f"`{name}` is reserved and cannot be a name")
        ty = parse_type_expr(body_toks[1:], line.lineno)
        self.i += 1
        return Decl(name, ty, line.lineno)

    def parse_block(self, parent_indent):
        """Parse the indented suite that follows a header at parent_indent."""
        if self.i >= len(self.lines) or self.lines[self.i].indent <= parent_indent:
            raise IonaError(self.lines[self.i - 1].lineno, "expected an indented block")
        block_indent = self.lines[self.i].indent
        stmts = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.indent < block_indent:
                break
            if line.indent != block_indent:
                raise IonaError(line.lineno, "inconsistent indentation")
            stmts.append(self.parse_statement(block_indent))
        return stmts

    def parse_statement(self, indent):
        line = self.lines[self.i]
        toks = line.tokens
        if is_def(toks):
            return self.parse_decl()
        if is_header(toks):
            kw = toks[-1].value          # `?` (if) or `&` (while)
            if kw == "?":
                self.i += 1
                then_body = self.parse_block(indent)
                else_body = None
                # optional `/` (else) at the same indentation
                if (self.i < len(self.lines)
                        and self.lines[self.i].indent == indent
                        and is_else(self.lines[self.i].tokens)):
                    self.i += 1
                    else_body = self.parse_block(indent)
                return If(toks[:-1], then_body, else_body, line.lineno)
            if kw == "&":
                self.i += 1
                body = self.parse_block(indent)
                return While(toks[:-1], body, line.lineno)
            raise IonaError(line.lineno, f"unknown block header ending in `{kw}`")
        self.i += 1
        return Stmt(toks, line.lineno)


# --------------------------------------------------------------------------
# Code generation: typed postfix -> C via a compile-time operand stack
# --------------------------------------------------------------------------

class Value:
    __slots__ = ("expr", "type")

    def __init__(self, expr, type):
        self.expr = expr   # a C expression string
        self.type = type   # an Iona Type


# Condition expression tree (see gen_cond): conditions compile to jumping code.

class Num:
    __slots__ = ("text", "lineno")
    def __init__(self, text, lineno=0): self.text, self.lineno = text, lineno

class FloatNode:
    __slots__ = ("text", "lineno")
    def __init__(self, text, lineno=0): self.text, self.lineno = text, lineno

class StrNode:
    __slots__ = ("s", "lineno")
    def __init__(self, s, lineno=0): self.s, self.lineno = s, lineno

class VarNode:
    __slots__ = ("name", "lineno")
    def __init__(self, name, lineno=0): self.name, self.lineno = name, lineno

class BinNode:        # arithmetic: a op b
    __slots__ = ("op", "a", "b", "lineno")
    def __init__(self, op, a, b, lineno=0): self.op, self.a, self.b, self.lineno = op, a, b, lineno

class ConvNode:       # explicit conversion: arg NAME  (e.g. X W2F)
    __slots__ = ("name", "arg", "lineno")
    def __init__(self, name, arg, lineno=0): self.name, self.arg, self.lineno = name, arg, lineno

class CallNode:       # f(args...)
    __slots__ = ("name", "args", "lineno")
    def __init__(self, name, args, lineno=0): self.name, self.args, self.lineno = name, args, lineno

class RelNode:        # boolean leaf: a <cmp> b
    __slots__ = ("op", "a", "b", "lineno")
    def __init__(self, op, a, b, lineno=0): self.op, self.a, self.b, self.lineno = op, a, b, lineno

class AndNode:
    __slots__ = ("l", "r", "lineno")
    def __init__(self, l, r, lineno=0): self.l, self.r, self.lineno = l, r, lineno

class OrNode:
    __slots__ = ("l", "r", "lineno")
    def __init__(self, l, r, lineno=0): self.l, self.r, self.lineno = l, r, lineno

class NotNode:
    __slots__ = ("x", "lineno")
    def __init__(self, x, lineno=0): self.x, self.lineno = x, lineno


def arith_result(op, ta, tb, lineno):
    """Type-check a binary arithmetic op; return the (shared) result type."""
    if ta != tb:
        raise IonaError(lineno, f"`{op}` needs both operands the same type "
                                f"(got {ta.iname()} and {tb.iname()})")
    if ta not in (BYTE, WORD, FLOAT):
        raise IonaError(lineno, f"`{op}` needs a numeric type (B, W, or F), got {ta.iname()}")
    if op == "%" and ta == FLOAT:
        raise IonaError(lineno, "`%` needs an integer type (B or W)")
    return ta


def cmp_check(op, ta, tb, lineno):
    if ta != tb:
        raise IonaError(lineno, f"comparison `{op}` needs both operands the same type "
                                f"(got {ta.iname()} and {tb.iname()})")


class FuncCtx:
    """Per-function code-generation state."""

    def __init__(self, params, ret):
        self.lines = []
        self.vars = {pn: pt for pn, pt in params}   # name -> Type
        self.ret = ret
        self.tmp = 0
        self.lbl = 0

    def emit(self, line):
        self.lines.append(line)

    def new_tmp(self):
        self.tmp += 1
        return f"_t{self.tmp}"

    def new_label(self):
        self.lbl += 1
        return f"_L{self.lbl}"


class CodeGen:
    def __init__(self, defs):
        self.defs = defs
        self.funcs = {d.name: d for d in defs}

    def generate(self):
        out = ["#include <stdio.h>", ""]
        for d in self.defs:
            out.append(self.signature(d) + ";")
        out.append("")
        for d in self.defs:
            out.extend(self.gen_func(d))
            out.append("")
        return "\n".join(out)

    @staticmethod
    def cname(name):
        # C's entry point must be the lowercase `main`; other Iona names are
        # already valid (uppercase) C identifiers and are used verbatim.
        return "main" if name == "MAIN" else name

    def signature(self, d):
        if d.name == "MAIN":
            if d.ret != WORD:
                raise IonaError(d.lineno, "MAIN must return W")
            return "int main(void)"
        params = ", ".join(f"{ctype(pt)} {pn}" for pn, pt in d.params) or "void"
        return f"{ctype(d.ret)} {self.cname(d.name)}({params})"

    def gen_func(self, d):
        ctx = FuncCtx(d.params, d.ret)
        self.gen_body(d.body, ctx, indent=1)
        head = [self.signature(d) + " {"]
        # `@` only *sets* the result; the function always falls through to a
        # single exit, so cleanup placed after a `@` still runs.
        if d.name == "MAIN":
            return head + ["    int _ret = 0;"] + ctx.lines + ["    return _ret;", "}"]
        if d.ret == VOID:
            return head + ctx.lines + ["}"]
        ret_decl = [f"    {ctype(d.ret)} _ret = {zero_init(d.ret)};"]
        return head + ret_decl + ctx.lines + ["    return _ret;", "}"]

    def gen_body(self, stmts, ctx, indent):
        for s in stmts:
            self.gen_stmt(s, ctx, indent)

    def gen_stmt(self, s, ctx, indent):
        pad = "    " * indent
        if isinstance(s, Decl):
            if s.name in ctx.vars:
                raise IonaError(s.lineno, f"`{s.name}` is already declared")
            ctx.vars[s.name] = s.type
            ctx.emit(f"{pad}{ctype(s.type)} {s.name} = {zero_init(s.type)};")
            return
        if isinstance(s, Stmt):
            self.compile_tokens(s.tokens, ctx, [], indent, allow_effects=True)
            return
        if isinstance(s, If):
            node = self.compile_cond(s.cond)
            if s.else_body is not None:
                l_else = ctx.new_label()
                l_end = ctx.new_label()
                self.gen_cond(node, ctx, None, l_else, indent)
                self.gen_body(s.then_body, ctx, indent + 1)
                ctx.emit(f"{pad}goto {l_end};")
                ctx.emit(f"{pad}{l_else}: ;")
                self.gen_body(s.else_body, ctx, indent + 1)
                ctx.emit(f"{pad}{l_end}: ;")
            else:
                l_end = ctx.new_label()
                self.gen_cond(node, ctx, None, l_end, indent)
                self.gen_body(s.then_body, ctx, indent + 1)
                ctx.emit(f"{pad}{l_end}: ;")
            return
        if isinstance(s, While):
            l_top = ctx.new_label()
            l_end = ctx.new_label()
            ctx.emit(f"{pad}{l_top}: ;")
            node = self.compile_cond(s.cond)
            self.gen_cond(node, ctx, None, l_end, indent)
            self.gen_body(s.body, ctx, indent + 1)
            ctx.emit(f"{pad}goto {l_top};")
            ctx.emit(f"{pad}{l_end}: ;")
            return
        if isinstance(s, FuncDef):
            raise IonaError(s.lineno, "nested function definitions are not supported")
        raise IonaError(0, f"internal: unknown statement {s!r}")

    def check_args(self, d, args, lineno):
        if len(args) != len(d.params):
            raise IonaError(lineno, f"`{d.name}` takes {len(d.params)} argument(s), got {len(args)}")
        for k, (a, (pn, pt)) in enumerate(zip(args, d.params), start=1):
            if a.type != pt:
                raise IonaError(lineno, f"argument {k} of `{d.name}` expects "
                                        f"{pt.iname()}, got {a.type.iname()}")

    # ---- conditions: build a boolean tree, then lower it to jumping code ----

    def compile_cond(self, tokens):
        """Build a boolean expression tree from a postfix condition (emits no code)."""
        stack = []
        for t in tokens:
            if t.kind == "int":
                stack.append(Num(t.value, t.lineno))
            elif t.kind == "float":
                stack.append(FloatNode(t.value, t.lineno))
            elif t.kind == "str":
                stack.append(StrNode(t.value, t.lineno))
            elif t.kind == "op":
                if t.value == "!":
                    raise IonaError(t.lineno, "assignment is not allowed in a condition")
                if t.value == "@":
                    raise IonaError(t.lineno, "`@` (return) is not allowed in a condition")
                if len(stack) < 2:
                    raise IonaError(t.lineno, f"operator `{t.value}` needs two operands")
                b = stack.pop()
                a = stack.pop()
                if t.value in COMPARES:
                    stack.append(RelNode(t.value, a, b, t.lineno))
                else:
                    stack.append(BinNode(t.value, a, b, t.lineno))
            elif t.kind == "name":
                name = t.value
                if name == "NOT":
                    if not stack:
                        raise IonaError(t.lineno, "`NOT` needs one operand")
                    stack.append(NotNode(self.as_bool(stack.pop()), t.lineno))
                elif name in ("AND", "OR"):
                    if len(stack) < 2:
                        raise IonaError(t.lineno, f"`{name}` needs two operands")
                    b = self.as_bool(stack.pop())
                    a = self.as_bool(stack.pop())
                    cls = AndNode if name == "AND" else OrNode
                    stack.append(cls(a, b, t.lineno))
                elif name == "PRINT":
                    raise IonaError(t.lineno, "`PRINT` cannot be used in a condition")
                elif name in CONVERSIONS:
                    if not stack:
                        raise IonaError(t.lineno, f"`{name}` needs one operand")
                    stack.append(ConvNode(name, stack.pop(), t.lineno))
                elif name in self.funcs:
                    d = self.funcs[name]
                    if len(stack) < len(d.params):
                        raise IonaError(t.lineno, f"`{name}` needs {len(d.params)} argument(s)")
                    args = [stack.pop() for _ in range(len(d.params))][::-1]
                    stack.append(CallNode(name, args, t.lineno))
                else:
                    stack.append(VarNode(name, t.lineno))
            else:
                raise IonaError(t.lineno, f"unexpected token {t.value!r}")
        if len(stack) != 1:
            ln = tokens[0].lineno if tokens else 0
            raise IonaError(ln, f"condition must produce exactly one value (got {len(stack)})")
        return self.as_bool(stack[0])

    def as_bool(self, node):
        """Treat a plain value as a truth test: nonzero is true (must be W)."""
        if isinstance(node, (RelNode, AndNode, OrNode, NotNode)):
            return node
        return RelNode("<>", node, Num("0", node.lineno), node.lineno)

    def gen_cond(self, node, ctx, t_lbl, f_lbl, indent):
        """Emit jumping code: branch to t_lbl when true, f_lbl when false.

        A `None` label means "fall through" -- the standard optimization that
        collapses output to a tight chain of compare-and-branch instructions.
        """
        pad = "    " * indent
        if isinstance(node, NotNode):
            self.gen_cond(node.x, ctx, f_lbl, t_lbl, indent)
            return
        if isinstance(node, AndNode):
            after = None
            lf = f_lbl
            if lf is None:
                after = ctx.new_label()
                lf = after
            self.gen_cond(node.l, ctx, None, lf, indent)
            self.gen_cond(node.r, ctx, t_lbl, f_lbl, indent)
            if after is not None:
                ctx.emit(f"{pad}{after}: ;")
            return
        if isinstance(node, OrNode):
            after = None
            lt = t_lbl
            if lt is None:
                after = ctx.new_label()
                lt = after
            self.gen_cond(node.l, ctx, lt, None, indent)
            self.gen_cond(node.r, ctx, t_lbl, f_lbl, indent)
            if after is not None:
                ctx.emit(f"{pad}{after}: ;")
            return
        self.gen_rel(node, ctx, t_lbl, f_lbl, indent)

    def gen_rel(self, node, ctx, t_lbl, f_lbl, indent):
        pad = "    " * indent
        a = self.emit_value(node.a, ctx, pad)
        b = self.emit_value(node.b, ctx, pad)
        cmp_check(node.op, a.type, b.type, node.lineno)
        cmp = f"({a.expr} {C_OP.get(node.op, node.op)} {b.expr})"
        if t_lbl is not None and f_lbl is not None:
            ctx.emit(f"{pad}if {cmp} goto {t_lbl};")
            ctx.emit(f"{pad}goto {f_lbl};")
        elif t_lbl is not None:
            ctx.emit(f"{pad}if {cmp} goto {t_lbl};")
        elif f_lbl is not None:
            ctx.emit(f"{pad}if (!{cmp}) goto {f_lbl};")

    def emit_value(self, node, ctx, pad):
        """Lower a value node to a typed C expression, emitting call temporaries
        at the point in the jumping code where the branch is reached."""
        if isinstance(node, Num):
            return Value(node.text, WORD)
        if isinstance(node, FloatNode):
            return Value(node.text, FLOAT)
        if isinstance(node, StrNode):
            return Value(self.c_string(node.s), Ptr(BYTE))
        if isinstance(node, VarNode):
            if node.name not in ctx.vars:
                raise IonaError(node.lineno, f"undeclared name `{node.name}`")
            return Value(node.name, ctx.vars[node.name])
        if isinstance(node, BinNode):
            a = self.emit_value(node.a, ctx, pad)
            b = self.emit_value(node.b, ctx, pad)
            rt = arith_result(node.op, a.type, b.type, node.lineno)
            return Value(f"({a.expr} {C_OP.get(node.op, node.op)} {b.expr})", rt)
        if isinstance(node, RelNode):
            a = self.emit_value(node.a, ctx, pad)
            b = self.emit_value(node.b, ctx, pad)
            cmp_check(node.op, a.type, b.type, node.lineno)
            return Value(f"({a.expr} {C_OP.get(node.op, node.op)} {b.expr})", WORD)
        if isinstance(node, ConvNode):
            v = self.emit_value(node.arg, ctx, pad)
            src, dst, cast = CONVERSIONS[node.name]
            if v.type != src:
                raise IonaError(node.lineno, f"`{node.name}` expects {src.iname()}, got {v.type.iname()}")
            return Value(f"({cast})({v.expr})", dst)
        if isinstance(node, CallNode):
            d = self.funcs[node.name]
            args = [self.emit_value(a, ctx, pad) for a in node.args]
            self.check_args(d, args, node.lineno)
            if d.ret == VOID:
                raise IonaError(node.lineno, f"`{node.name}` returns nothing and has no value here")
            call = f"{self.cname(node.name)}(" + ", ".join(a.expr for a in args) + ")"
            tmp = ctx.new_tmp()
            ctx.emit(f"{pad}{ctype(d.ret)} {tmp} = {call};")
            return Value(tmp, d.ret)
        raise IonaError(getattr(node, "lineno", 0), "internal: bad value node")

    def compile_tokens(self, tokens, ctx, stack, indent, allow_effects):
        pad = "    " * indent
        for t in tokens:
            if t.kind == "int":
                stack.append(Value(t.value, WORD))
            elif t.kind == "float":
                stack.append(Value(t.value, FLOAT))
            elif t.kind == "str":
                stack.append(Value(self.c_string(t.value), Ptr(BYTE)))
            elif t.kind == "op":
                if t.value == "!":
                    self.op_assign(t, ctx, stack, pad)
                elif t.value == "@":
                    self.op_return(t, ctx, stack, pad)
                else:
                    self.op_binary(t, stack)
            elif t.kind == "name":
                self.compile_name(t, ctx, stack, pad)
            else:
                raise IonaError(t.lineno, f"unexpected token {t.value!r}")

    def op_binary(self, t, stack):
        if len(stack) < 2:
            raise IonaError(t.lineno, f"operator `{t.value}` needs two operands")
        b = stack.pop()
        a = stack.pop()
        cop = C_OP.get(t.value, t.value)
        if t.value in COMPARES:
            cmp_check(t.value, a.type, b.type, t.lineno)
            stack.append(Value(f"({a.expr} {cop} {b.expr})", WORD))
        else:
            rt = arith_result(t.value, a.type, b.type, t.lineno)
            stack.append(Value(f"({a.expr} {cop} {b.expr})", rt))

    def op_assign(self, t, ctx, stack, pad):
        if len(stack) < 2:
            raise IonaError(t.lineno, "assignment needs a value and a target")
        target = stack.pop()
        value = stack.pop()
        name = target.expr
        if not _NAME_RE.fullmatch(name):
            raise IonaError(t.lineno, "assignment target must be a variable name")
        if name not in ctx.vars:
            raise IonaError(t.lineno, f"undeclared variable `{name}` (declare it with `!{name} TYPE`)")
        if value.type != ctx.vars[name]:
            raise IonaError(t.lineno, f"cannot assign {value.type.iname()} to `{name}` "
                                      f"of type {ctx.vars[name].iname()}")
        ctx.emit(f"{pad}{name} = {value.expr};")

    def op_return(self, t, ctx, stack, pad):
        # `@` sets the result and keeps executing (cleanup after it still runs).
        if ctx.ret == VOID:
            if stack:
                raise IonaError(t.lineno, "a `V` (void) function cannot return a value")
            return
        if not stack:
            raise IonaError(t.lineno, f"`@` needs a value (this function returns {ctx.ret.iname()})")
        v = stack.pop()
        if v.type != ctx.ret:
            raise IonaError(t.lineno, f"`@` returns {ctx.ret.iname()} but the value is {v.type.iname()}")
        ctx.emit(f"{pad}_ret = {v.expr};")

    def compile_name(self, t, ctx, stack, pad):
        name = t.value
        if name in LOGICAL:
            raise IonaError(t.lineno, f"`{name}` is a logical operator and is only allowed in a condition")
        if name == "PRINT":
            if not stack:
                raise IonaError(t.lineno, "`PRINT` needs a value")
            v = stack.pop()
            if v.type == WORD:
                fmt = "%zu"
            elif v.type == BYTE:
                fmt = "%d"
            elif v.type == FLOAT:
                fmt = "%g"
            elif v.type == Ptr(BYTE):
                fmt = "%s"
            else:
                raise IonaError(t.lineno, f"cannot PRINT a value of type {v.type.iname()}")
            ctx.emit(f'{pad}printf("{fmt}\\n", {v.expr});')
            return
        if name in CONVERSIONS:
            if not stack:
                raise IonaError(t.lineno, f"`{name}` needs one operand")
            v = stack.pop()
            src, dst, cast = CONVERSIONS[name]
            if v.type != src:
                raise IonaError(t.lineno, f"`{name}` expects {src.iname()}, got {v.type.iname()}")
            stack.append(Value(f"({cast})({v.expr})", dst))
            return
        if name in self.funcs:
            d = self.funcs[name]
            if len(stack) < len(d.params):
                raise IonaError(t.lineno, f"`{name}` needs {len(d.params)} argument(s)")
            args = [stack.pop() for _ in range(len(d.params))][::-1]
            self.check_args(d, args, t.lineno)
            call = f"{self.cname(name)}(" + ", ".join(a.expr for a in args) + ")"
            if d.ret == VOID:
                ctx.emit(f"{pad}{call};")
                return
            # Materialize into a temp so the call's evaluation order is pinned.
            tmp = ctx.new_tmp()
            ctx.emit(f"{pad}{ctype(d.ret)} {tmp} = {call};")
            stack.append(Value(tmp, d.ret))
            return
        if name in ctx.vars:
            stack.append(Value(name, ctx.vars[name]))
            return
        raise IonaError(t.lineno, f"undeclared name `{name}`")

    @staticmethod
    def c_string(s):
        out = ['"']
        for ch in s:
            if ch == '"':
                out.append('\\"')
            elif ch == "\\":
                out.append("\\\\")
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        out.append('"')
        return "".join(out)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def compile_source(src):
    lines = read_lines(src)
    defs = Parser(lines).parse_module()
    if "MAIN" not in {d.name for d in defs}:
        raise IonaError(0, "program has no `MAIN` function")
    return CodeGen(defs).generate()


def main(argv):
    if len(argv) < 2:
        print("usage: ionac.py <source.iona> [-o output] [-c] [-r]", file=sys.stderr)
        print("  -c   emit C only (to <output>.c), do not invoke the C compiler", file=sys.stderr)
        print("  -r   run the program after building", file=sys.stderr)
        return 2

    args = argv[1:]
    src_path = None
    out_path = None
    emit_c_only = False
    run = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            out_path = args[i + 1]
            i += 2
        elif a == "-c":
            emit_c_only = True
            i += 1
        elif a == "-r":
            run = True
            i += 1
        else:
            src_path = a
            i += 1

    if src_path is None:
        print("error: no source file given", file=sys.stderr)
        return 2

    with open(src_path) as f:
        src = f.read()

    try:
        c_code = compile_source(src)
    except IonaError as e:
        where = f"{src_path}:{e.lineno}" if e.lineno else src_path
        print(f"{where}: error: {e.msg}", file=sys.stderr)
        return 1

    base = os.path.splitext(src_path)[0]
    c_path = (out_path + ".c") if (out_path and emit_c_only) else base + ".c"
    if emit_c_only and out_path:
        c_path = out_path
    with open(c_path, "w") as f:
        f.write(c_code)

    if emit_c_only:
        print(c_path)
        return 0

    exe_path = out_path or base
    cc = os.environ.get("CC", "cc")
    proc = subprocess.run([cc, c_path, "-o", exe_path])
    if proc.returncode != 0:
        print("error: C compiler failed", file=sys.stderr)
        return 1

    if run:
        return subprocess.run([os.path.abspath(exe_path)]).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
