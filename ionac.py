#!/usr/bin/env python3
"""ionac - a compiler for the Iona programming language.

Iona is an experimental systems language:
  - Procedural, with block structure defined by indentation (like Python).
  - Within a statement, the syntax is postfix (like Forth): operands precede
    the operator, so `3 4 +` means 3 + 4 and `N FACTORIAL` calls FACTORIAL(N).
  - Compiles directly to machine code (this v0 emits C and hands it to `cc`).
  - No garbage collection: locals are plain machine words.
  - Source is UPPERCASE with terse operators (`=` equality, `<>` not-equal,
    `!` assignment), staying within a 1960s teletype's character set.

This file is the whole toolchain for the v0 language: tokenizer, an
indentation-based block parser, and a code generator that lowers postfix
statements to C by maintaining a *compile-time* operand stack of C
expressions.  See README.md for the language reference.
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
# assignment when it trails a name, and the definition marker when it leads a
# line. `?` trails a condition to mark a conditional, `&` trails one to mark a
# while loop, and `@` trails a value to return it. These markers are lexed here
# as `op` tokens; the parser and code generator interpret them by position.
# Multi-character operators must be tried before their single-char prefixes.
OPERATORS = ["<=", ">=", "<>", "<", ">", "=", "!", "?", "@", "&", "+", "-", "*", "/", "%"]
BINOPS = {"<=", ">=", "<>", "<", ">", "=", "+", "-", "*", "/", "%"}
COMPARES = {"<=", ">=", "<>", "<", ">", "="}

# C spellings for the operators whose Iona form differs.
C_OP = {"=": "==", "<>": "!="}

# Short-circuit logical connectives. They are condition-only control-flow
# words (lowered to branches), deliberately *not* value-producing operators --
# bitwise operators will get their own spelling. Uppercase like all keywords:
# a 1960s teletype (the ASR-33) had no lowercase letters at all.
LOGICAL = {"AND", "OR", "NOT"}

# Identifiers are UPPERCASE letters and digits only. A 1960s teletype (the
# ASR-33) printed no lowercase, and its 0x5F position was a left-arrow, not `_`,
# so both lowercase and underscore are excluded for historical plausibility.
# Excluding underscore also keeps user names clear of the compiler's own
# `_`-prefixed temporaries and labels.
_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*")
_INT_RE = re.compile(r"[0-9]+")


class Token:
    __slots__ = ("kind", "value", "lineno")

    def __init__(self, kind, value, lineno):
        self.kind = kind  # 'int' | 'str' | 'op' | 'name'
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
# AST
# --------------------------------------------------------------------------

class FuncDef:
    def __init__(self, name, params, body, lineno):
        self.name = name
        self.params = params
        self.body = body
        self.lineno = lineno


class If:
    def __init__(self, cond, then_body, else_body, lineno):
        self.cond = cond          # list[Token]
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
    """A definition header: `!` prefixed to the function name, e.g. `!FIB N`.

    The prefix `!` is always line-initial, so it never collides with the
    postfix `!` of assignment (`VALUE NAME!`), which always follows its operands.
    """
    return tokens[0].kind == "op" and tokens[0].value == "!"


def is_else(tokens):
    """The else branch: a line that is just `/`.

    A lone `/` is never a valid statement (division needs two operands), so it
    is unambiguous as the else marker even though `/` is also the division
    operator when it sits between operands inside an expression.
    """
    return len(tokens) == 1 and tokens[0].kind == "op" and tokens[0].value == "/"


def is_header(tokens):
    """True if the line opens an indented block.

    Headers carry no colon -- a marker is enough, and the indented suite that
    follows delimits the body: `!` leads a definition, `?` and `&` trail a
    condition (conditional and while loop), and a lone `/` is the else branch.
    """
    if is_def(tokens):
        return True
    last = tokens[-1]
    if last.kind == "op" and last.value in ("?", "&"):
        return True
    return is_else(tokens)


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
        toks = line.tokens
        # !NAME PARAM1 PARAM2 ...   (toks[0] is the `!` marker)
        names = [t.value for t in toks[1:]]
        if not toks[1:] or any(t.kind != "name" for t in toks[1:]):
            raise IonaError(line.lineno, "malformed declaration: expected `!NAME PARAMS`")
        for nm in names:
            if nm in LOGICAL:
                raise IonaError(line.lineno, f"`{nm}` is a reserved logical operator and cannot be a name")
        name, params = names[0], names[1:]
        self.i += 1
        body = self.parse_block(line.indent)
        return FuncDef(name, params, body, line.lineno)

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
        if is_header(toks):
            if is_def(toks):
                return self.parse_def()
            kw = toks[-1].value          # `?` (if) or `&` (while); condition is everything before
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
# Code generation: postfix -> C via a compile-time operand stack
# --------------------------------------------------------------------------

class Value:
    __slots__ = ("expr", "type")

    def __init__(self, expr, type):
        self.expr = expr   # a C expression string
        self.type = type   # 'int' | 'str'


# --------------------------------------------------------------------------
# Condition expression tree
# --------------------------------------------------------------------------
# A condition is NOT compiled to a 0/1 value. It is built into a small tree
# (value leaves + relational leaves + and/or/not nodes) by `compile_cond`,
# then lowered by `gen_cond` as *jumping code*: each piece branches straight to
# a true-label or false-label. That single mechanism gives both short-circuit
# evaluation and fused compare-and-branch output, and -- because a leaf's code
# (including any function call) is only emitted where its branch is reached --
# calls on a short-circuited path never execute.

class Num:
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


class FuncCtx:
    """Per-function code-generation state."""

    def __init__(self, params):
        self.lines = []          # emitted C statement lines (body)
        self.declared = set(params)
        self.locals = []         # extra locals to hoist (name)
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
        # forward declarations
        for d in self.defs:
            out.append(self.signature(d) + ";")
        out.append("")
        for d in self.defs:
            out.extend(self.gen_func(d))
            out.append("")
        return "\n".join(out)

    @staticmethod
    def cname(name):
        # C's entry point must be the lowercase `main`; every other Iona name is
        # already a valid (uppercase) C identifier and is used verbatim.
        return "main" if name == "MAIN" else name

    def signature(self, d):
        if d.name == "MAIN" and not d.params:
            return "int main(void)"
        params = ", ".join(f"int {p}" for p in d.params)
        return f"int {self.cname(d.name)}({params})"

    def gen_func(self, d):
        ctx = FuncCtx(d.params)
        self.gen_body(d.body, ctx, indent=1)
        # `@` only *sets* the result; the function always falls through to this
        # single exit point, so cleanup code placed after a `@` still runs (Iona
        # has no destructors). `_ret` defaults to 0.
        head = [self.signature(d) + " {"]
        ret_decl = ["    int _ret = 0;"]
        decls = [f"    int {name};" for name in ctx.locals]
        tail = ["    return _ret;", "}"]
        return head + ret_decl + decls + ctx.lines + tail

    def gen_body(self, stmts, ctx, indent):
        for s in stmts:
            self.gen_stmt(s, ctx, indent)

    def gen_stmt(self, s, ctx, indent):
        pad = "    " * indent
        if isinstance(s, Stmt):
            stack = []
            self.compile_tokens(s.tokens, ctx, stack, indent, allow_effects=True)
            # leftover pure expressions are simply discarded
            return
        if isinstance(s, If):
            # Jumping code: the condition branches to a false-label; the true
            # case falls through into the then-body.
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
            # The condition (including any call temporaries it emits) lives
            # after the top label, so it is re-evaluated every iteration.
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

    # ---- conditions: build a boolean tree, then lower it to jumping code ----

    def compile_cond(self, tokens):
        """Build a boolean expression tree from a postfix condition.

        This is a pure compile-time pass: it emits no code, only assembles
        nodes on a stack. `AND`/`OR`/`NOT` are control-flow words, valid only
        here; assignment, `@` (return), and `PRINT` are rejected.
        """
        stack = []
        for t in tokens:
            if t.kind == "int":
                stack.append(Num(t.value, t.lineno))
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
                elif name in self.funcs:
                    d = self.funcs[name]
                    arity = len(d.params)
                    if len(stack) < arity:
                        raise IonaError(t.lineno, f"`{name}` needs {arity} argument(s)")
                    args = [stack.pop() for _ in range(arity)][::-1]
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
        """Treat a plain value as a truth test: nonzero is true."""
        if isinstance(node, (RelNode, AndNode, OrNode, NotNode)):
            return node
        return RelNode("!=", node, Num("0", node.lineno), node.lineno)

    def gen_cond(self, node, ctx, t_lbl, f_lbl, indent):
        """Emit jumping code for a boolean tree.

        Control reaches `t_lbl` when the condition is true and `f_lbl` when it
        is false. Either label may be `None`, meaning "fall through" -- that is
        the standard fall-through optimization, and it is what collapses the
        output down to a tight chain of compare-and-branch instructions.
        """
        pad = "    " * indent
        if isinstance(node, NotNode):
            self.gen_cond(node.x, ctx, f_lbl, t_lbl, indent)
            return
        if isinstance(node, AndNode):
            # left false => whole false; left true => test right (fall through).
            after = None
            lf = f_lbl
            if lf is None:                 # false must skip past the right operand
                after = ctx.new_label()
                lf = after
            self.gen_cond(node.l, ctx, None, lf, indent)
            self.gen_cond(node.r, ctx, t_lbl, f_lbl, indent)
            if after is not None:
                ctx.emit(f"{pad}{after}: ;")
            return
        if isinstance(node, OrNode):
            # left true => whole true; left false => test right (fall through).
            after = None
            lt = t_lbl
            if lt is None:                 # true must skip past the right operand
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
        if a.type != "int" or b.type != "int":
            raise IonaError(node.lineno, f"comparison `{node.op}` requires integers")
        cmp = f"({a.expr} {C_OP.get(node.op, node.op)} {b.expr})"
        if t_lbl is not None and f_lbl is not None:
            ctx.emit(f"{pad}if {cmp} goto {t_lbl};")
            ctx.emit(f"{pad}goto {f_lbl};")
        elif t_lbl is not None:            # fall through when false
            ctx.emit(f"{pad}if {cmp} goto {t_lbl};")
        elif f_lbl is not None:            # fall through when true
            ctx.emit(f"{pad}if (!{cmp}) goto {f_lbl};")
        # both None: nothing to branch on (degenerate)

    def emit_value(self, node, ctx, pad):
        """Lower a value node to a C expression, emitting any call temporaries.

        Calls are materialized here -- at the point in the jumping code where
        the branch is actually reached -- so a call guarded by a short-circuit
        is never evaluated on the path that skips it.
        """
        if isinstance(node, Num):
            return Value(node.text, "int")
        if isinstance(node, StrNode):
            return Value(self.c_string(node.s), "str")
        if isinstance(node, VarNode):
            return Value(node.name, "int")
        if isinstance(node, BinNode):
            a = self.emit_value(node.a, ctx, pad)
            b = self.emit_value(node.b, ctx, pad)
            if a.type != "int" or b.type != "int":
                raise IonaError(node.lineno, f"operator `{node.op}` requires integers")
            return Value(f"({a.expr} {C_OP.get(node.op, node.op)} {b.expr})", "int")
        if isinstance(node, RelNode):
            a = self.emit_value(node.a, ctx, pad)
            b = self.emit_value(node.b, ctx, pad)
            if a.type != "int" or b.type != "int":
                raise IonaError(node.lineno, f"comparison `{node.op}` requires integers")
            return Value(f"({a.expr} {C_OP.get(node.op, node.op)} {b.expr})", "int")
        if isinstance(node, CallNode):
            args = [self.emit_value(a, ctx, pad) for a in node.args]
            call = f"{self.cname(node.name)}(" + ", ".join(a.expr for a in args) + ")"
            tmp = ctx.new_tmp()
            ctx.emit(f"{pad}int {tmp} = {call};")
            return Value(tmp, "int")
        raise IonaError(getattr(node, "lineno", 0), "internal: bad value node")

    def compile_tokens(self, tokens, ctx, stack, indent, allow_effects):
        pad = "    " * indent
        for t in tokens:
            if t.kind == "int":
                stack.append(Value(t.value, "int"))
            elif t.kind == "str":
                stack.append(Value(self.c_string(t.value), "str"))
            elif t.kind == "op":
                if t.value == "!":
                    self.op_assign(t, ctx, stack, pad, allow_effects)
                elif t.value == "@":
                    self.op_return(t, ctx, stack, pad, allow_effects)
                else:
                    self.op_binary(t, stack)
            elif t.kind == "name":
                self.compile_name(t, ctx, stack, pad, allow_effects)
            else:
                raise IonaError(t.lineno, f"unexpected token {t.value!r}")

    def op_binary(self, t, stack):
        if len(stack) < 2:
            raise IonaError(t.lineno, f"operator `{t.value}` needs two operands")
        b = stack.pop()
        a = stack.pop()
        if a.type != "int" or b.type != "int":
            raise IonaError(t.lineno, f"operator `{t.value}` requires integers")
        stack.append(Value(f"({a.expr} {C_OP.get(t.value, t.value)} {b.expr})", "int"))

    def op_assign(self, t, ctx, stack, pad, allow_effects):
        if not allow_effects:
            raise IonaError(t.lineno, "assignment is not allowed in this context")
        if len(stack) < 2:
            raise IonaError(t.lineno, "assignment needs a value and a target")
        target = stack.pop()
        value = stack.pop()
        # The target was pushed as a bare name; recover it from its expression.
        name = target.expr
        if not _NAME_RE.fullmatch(name):
            raise IonaError(t.lineno, "assignment target must be a variable name")
        if name not in ctx.declared:
            ctx.declared.add(name)
            ctx.locals.append(name)
        ctx.emit(f"{pad}{name} = {value.expr};")

    def op_return(self, t, ctx, stack, pad, allow_effects):
        if not allow_effects:
            raise IonaError(t.lineno, "`@` (return) is not allowed in this context")
        # `@` only *sets* the result and keeps executing: any cleanup statements
        # after it still run before the function actually returns. With no
        # operand on the stack it returns 0.
        if stack:
            v = stack.pop()
            ctx.emit(f"{pad}_ret = {v.expr};")
        else:
            ctx.emit(f"{pad}_ret = 0;")

    def compile_name(self, t, ctx, stack, pad, allow_effects):
        name = t.value
        if name in LOGICAL:
            raise IonaError(t.lineno, f"`{name}` is a logical operator and is only allowed in a condition")
        if name == "PRINT":
            if not allow_effects:
                raise IonaError(t.lineno, "`PRINT` cannot be used in a condition")
            if not stack:
                raise IonaError(t.lineno, "`PRINT` needs a value")
            v = stack.pop()
            fmt = "%s" if v.type == "str" else "%d"
            ctx.emit(f'{pad}printf("{fmt}\\n", {v.expr});')
            return
        if name in self.funcs:
            d = self.funcs[name]
            arity = len(d.params)
            if len(stack) < arity:
                raise IonaError(t.lineno, f"`{name}` needs {arity} argument(s)")
            args = [stack.pop() for _ in range(arity)][::-1]
            call = f"{self.cname(name)}(" + ", ".join(a.expr for a in args) + ")"
            # Materialize into a temp so the call's evaluation order is pinned.
            tmp = ctx.new_tmp()
            ctx.emit(f"{pad}int {tmp} = {call};")
            stack.append(Value(tmp, "int"))
            return
        # otherwise: a variable reference
        stack.append(Value(name, "int"))

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
