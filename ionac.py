#!/usr/bin/env python3
"""ionac - a compiler for the Iona programming language.

Iona is an experimental systems language:
  - Procedural, with block structure defined by indentation (like Python).
  - Within a statement, the syntax is postfix (like Forth): operands precede
    the operator, so `3 4 +` means 3 + 4 and `n factorial` calls factorial(n).
  - Compiles directly to machine code (this v0 emits C and hands it to `cc`).
  - No garbage collection: locals are plain machine words.

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

# Multi-character operators must be tried before their single-char prefixes.
OPERATORS = ["<=", ">=", "==", "!=", "<", ">", "=", "+", "-", "*", "/", "%"]
BINOPS = {"<=", ">=", "==", "!=", "<", ">", "+", "-", "*", "/", "%"}

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INT_RE = re.compile(r"[0-9]+")


class Token:
    __slots__ = ("kind", "value", "lineno")

    def __init__(self, kind, value, lineno):
        self.kind = kind  # 'int' | 'str' | 'op' | 'name' | 'colon'
        self.value = value
        self.lineno = lineno

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r})"


def tokenize_line(text, lineno):
    """Tokenize one physical line (comments already implied by `#`)."""
    toks = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t":
            i += 1
            continue
        if c == "#":
            break  # rest of line is a comment
        if c == ":":
            toks.append(Token("colon", ":", lineno))
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

def is_header(tokens):
    return tokens[-1].kind == "colon"


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
            if not (is_header(line.tokens) and line.tokens[0].value == "def"):
                raise IonaError(line.lineno, "only `def` declarations are allowed at top level")
            defs.append(self.parse_def())
        return defs

    def parse_def(self):
        line = self.lines[self.i]
        body_toks = line.tokens[:-1]  # drop colon
        # def name param1 param2 ...
        names = [t.value for t in body_toks[1:]]
        if not body_toks[1:] or any(t.kind != "name" for t in body_toks[1:]):
            raise IonaError(line.lineno, "malformed def header: expected `def name params:`")
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
            head = toks[:-1]  # without colon
            kw = head[-1].value if head else None
            if head and head[0].value == "def":
                return self.parse_def()
            if kw == "if":
                self.i += 1
                then_body = self.parse_block(indent)
                else_body = None
                # optional `else:` at the same indentation
                if (self.i < len(self.lines)
                        and self.lines[self.i].indent == indent
                        and is_header(self.lines[self.i].tokens)
                        and self.lines[self.i].tokens[0].value == "else"):
                    self.i += 1
                    else_body = self.parse_block(indent)
                return If(head[:-1], then_body, else_body, line.lineno)
            if kw == "while":
                self.i += 1
                body = self.parse_block(indent)
                return While(head[:-1], body, line.lineno)
            raise IonaError(line.lineno, f"unknown block header ending in `{kw}:`")
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


class FuncCtx:
    """Per-function code-generation state."""

    def __init__(self, params):
        self.lines = []          # emitted C statement lines (body)
        self.declared = set(params)
        self.locals = []         # extra locals to hoist (name)
        self.tmp = 0

    def emit(self, line):
        self.lines.append(line)

    def new_tmp(self):
        self.tmp += 1
        return f"_t{self.tmp}"


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

    def signature(self, d):
        if d.name == "main" and not d.params:
            return "int main(void)"
        params = ", ".join(f"int {p}" for p in d.params)
        return f"int {d.name}({params})"

    def gen_func(self, d):
        ctx = FuncCtx(d.params)
        self.gen_body(d.body, ctx, indent=1)
        if d.name == "main":
            ctx.emit("    return 0;")
        head = [self.signature(d) + " {"]
        decls = [f"    int {name};" for name in ctx.locals]
        return head + decls + ctx.lines + ["}"]

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
            cond = self.compile_condition(s.cond, ctx, indent)
            ctx.emit(f"{pad}if ({cond}) {{")
            self.gen_body(s.then_body, ctx, indent + 1)
            if s.else_body is not None:
                ctx.emit(f"{pad}}} else {{")
                self.gen_body(s.else_body, ctx, indent + 1)
            ctx.emit(f"{pad}}}")
            return
        if isinstance(s, While):
            # Re-evaluate the condition each iteration: its setup code (e.g. any
            # call temporaries) must run inside the loop, so we use the
            # `while (1) { setup; if (!cond) break; body }` shape.
            ctx.emit(f"{pad}while (1) {{")
            cond = self.compile_condition(s.cond, ctx, indent + 1)
            inner = "    " * (indent + 1)
            ctx.emit(f"{inner}if (!({cond})) break;")
            self.gen_body(s.body, ctx, indent + 1)
            ctx.emit(f"{pad}}}")
            return
        if isinstance(s, FuncDef):
            raise IonaError(s.lineno, "nested function definitions are not supported")
        raise IonaError(0, f"internal: unknown statement {s!r}")

    def compile_condition(self, tokens, ctx, indent):
        stack = []
        self.compile_tokens(tokens, ctx, stack, indent, allow_effects=False)
        if len(stack) != 1:
            ln = tokens[0].lineno if tokens else 0
            raise IonaError(ln, f"condition must produce exactly one value (got {len(stack)})")
        return stack[0].expr

    def compile_tokens(self, tokens, ctx, stack, indent, allow_effects):
        pad = "    " * indent
        for t in tokens:
            if t.kind == "int":
                stack.append(Value(t.value, "int"))
            elif t.kind == "str":
                stack.append(Value(self.c_string(t.value), "str"))
            elif t.kind == "op":
                if t.value == "=":
                    self.op_assign(t, ctx, stack, pad, allow_effects)
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
        stack.append(Value(f"({a.expr} {t.value} {b.expr})", "int"))

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

    def compile_name(self, t, ctx, stack, pad, allow_effects):
        name = t.value
        if name == "print":
            if not allow_effects:
                raise IonaError(t.lineno, "`print` cannot be used in a condition")
            if not stack:
                raise IonaError(t.lineno, "`print` needs a value")
            v = stack.pop()
            fmt = "%s" if v.type == "str" else "%d"
            ctx.emit(f'{pad}printf("{fmt}\\n", {v.expr});')
            return
        if name == "return":
            if not allow_effects:
                raise IonaError(t.lineno, "`return` cannot be used in a condition")
            if stack:
                v = stack.pop()
                ctx.emit(f"{pad}return {v.expr};")
            else:
                ctx.emit(f"{pad}return 0;")
            return
        if name in self.funcs:
            d = self.funcs[name]
            arity = len(d.params)
            if len(stack) < arity:
                raise IonaError(t.lineno, f"`{name}` needs {arity} argument(s)")
            args = [stack.pop() for _ in range(arity)][::-1]
            call = f"{name}(" + ", ".join(a.expr for a in args) + ")"
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
    if "main" not in {d.name for d in defs}:
        raise IonaError(0, "program has no `main` function")
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
