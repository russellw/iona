# Iona

Iona is an experimental systems programming language. It combines three ideas
that are not usually found together:

- **A systems language**, like C or Pascal: it compiles directly to machine
  code and has no garbage collector. Values are plain machine words.
- **Indentation-based block structure**, like Python: `if`, `while`, and `def`
  open an indented suite, with no `begin`/`end` or braces.
- **Postfix expressions within a line**, like Forth: operands come before the
  operator, so `3 4 +` means `3 + 4`, and `n factorial` calls `factorial(n)`.
  There are no parentheses and no operator-precedence rules to memorize.

This repository contains `ionac.py`, the v0 compiler. It is written in Python
and compiles Iona to C, then invokes the system C compiler (`cc`) to produce a
native executable.

## Quick start

```sh
# Build and run a program:
python3 ionac.py examples/factorial.iona -r

# Build an executable next to the source:
python3 ionac.py examples/factorial.iona -o factorial

# Emit C only (don't invoke the C compiler):
python3 ionac.py examples/factorial.iona -c -o factorial.c

# Run the test suite:
python3 tests/run_tests.py
```

## The language (v0)

A program is a sequence of `def` declarations. Execution begins at `main`.

```
def factorial n:
    n 1 <= if:
        1 return
    n  n 1 - factorial  *  return

def main:
    5 factorial print     # 120
```

### Lines are postfix token streams

Within a line, tokens are pushed onto an operand stack and operators consume
from it — exactly like Forth, but the stack is resolved at compile time, so the
generated code is ordinary efficient C with no runtime stack.

- `2 3 4 * +`  →  `2 + (3 * 4)`  →  `14`
- `n 1 -`  →  `n - 1`
- `a b max`  →  `max(a, b)`

### Declarations read prefix

Declarations are the one deliberate exception to postfix order, because they
name a thing rather than compute a value:

```
def name param1 param2:
    <body>
```

All parameters and values are integers in v0.

### Control flow reads postfix

The condition is evaluated first and the keyword consumes it:

```
n 0 > if:
    "positive" print
else:
    "not positive" print

i n < while:
    i print
    i 1 + i =
```

### Assignment

Assignment is itself a postfix operator: push a value, push a target name, then
`=`. Variables are declared automatically on first assignment.

```
5 x =          # x = 5
x 1 + x =      # x = x + 1
```

### Built-ins

| Form        | Meaning                                  |
|-------------|------------------------------------------|
| `+ - * / %` | integer arithmetic                       |
| `== != < > <= >=` | comparisons (yield `0` / `1`)      |
| `x print`   | print an integer or string, then newline |
| `v return`  | return `v` from the current function     |
| `return`    | return `0`                               |

### Literals and comments

- Integer literals: `0`, `42`, `3628800`.
- String literals: `"hello"` (usable as a `print` argument), with `\n`, `\t`,
  `\"`, `\\` escapes.
- Comments run from `#` to end of line.
- Indentation uses spaces; tabs are rejected.

## How it works

`ionac.py` is a single self-contained file with four stages:

1. **Tokenizer** — splits each physical line into postfix tokens.
2. **Line reader** — measures indentation and drops blank/comment lines.
3. **Block parser** — turns indentation into a nested AST of `def`, `if`,
   `while`, and statement nodes.
4. **Code generator** — lowers each postfix statement by walking the tokens
   with a *compile-time operand stack* of C expression strings. Function calls
   are materialized into temporaries so evaluation order is well defined.

## Status and roadmap

v0 is intentionally small but runs real recursive and iterative programs (see
`examples/` and `tests/`). Natural next steps:

- More types (`bool`, fixed-width ints, pointers, `char`/strings) with a real
  type checker rather than int-everywhere.
- `for` loops and an `and`/`or`/`not` for boolean logic.
- Struct / record types and manual memory (stack, arena, or `malloc`/`free`).
- Multiple return values and the stack-effect comments Forth is known for.
- A direct native backend (assembly or LLVM) to drop the C dependency.
