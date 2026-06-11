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

### Logical operators (conditions only)

`and`, `or`, and `not` combine conditions and **short-circuit**: `or` stops at
the first true operand, `and` at the first false one, and a guarded call on the
skipped side is never evaluated.

```
x 0 >  x 100 <  and if:        # 0 < x < 100
    "in range" print

p 0 ==  p valid  or if:        # `p valid` is not called when p == 0
    "ok" print

done not while:
    step
```

They are control-flow words, not value-producing operators, so they are allowed
**only** in the condition of an `if` or `while`. They do not double as bitwise
operators: bitwise `and`/`or`/`xor`/`not` will get their own symbolic spelling
(`&`, `|`, `^`, `~`) and remain ordinary value operators.

Under the hood a condition is compiled as *jumping code*: instead of computing a
`0`/`1` and testing it, each part branches straight to a true- or false-label.
That one mechanism yields both short-circuit evaluation and tight fused
compare-and-branch output, with no boolean ever materialized in a register.

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
| `and or not` | short-circuit logical operators (conditions only) |
| `x print`   | print an integer or string, then newline |
| `v return`  | set the function's return value to `v`   |
| `return`    | set the function's return value to `0`   |

### Return values and cleanup

Iona has no destructors, so cleanup code (closing files, freeing buffers) must
run explicitly. To make sure it always gets a chance, **`return` does not exit
the function** — it only *sets* the value to be returned. Execution continues
to the end of the function, which is the single point where it actually
returns. Anything after a `return` still runs:

```
def read_squared x:
    "open file" print
    x x * return          # set the result
    "close file" print    # still runs -- cleanup is never skipped

# prints: open file / close file / then main prints 25 for read_squared(5)
```

A consequence: because `return` no longer skips the rest of the function, use
`if`/`else` (not a bare early `return`) when one branch must not run the other.
A function's return value defaults to `0` if `return` is never reached.

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
   Conditions take a separate path: they are built into a small boolean tree
   and emitted as *jumping code* (branches to true/false labels) so that
   `and`/`or`/`not` short-circuit and compile to fused compare-and-branch.

## Status and roadmap

v0 is intentionally small but runs real recursive and iterative programs (see
`examples/` and `tests/`). Natural next steps:

- More types (`bool`, fixed-width ints, pointers, `char`/strings) with a real
  type checker rather than int-everywhere.
- `for` loops; bitwise operators (`& | ^ ~`, `<<`/`>>`) as value operators,
  kept distinct from the short-circuit logical `and`/`or`/`not`.
- Struct / record types and manual memory (stack, arena, or `malloc`/`free`).
- Multiple return values and the stack-effect comments Forth is known for.
- A direct native backend (assembly or LLVM) to drop the C dependency.
