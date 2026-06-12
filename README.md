# Iona

Iona is an experimental systems programming language. It combines three ideas
that are not usually found together:

- **A systems language**, like C or Pascal: it compiles directly to machine
  code and has no garbage collector. Values are plain machine words.
- **Indentation-based block structure**, like Python but terser: a `!`-prefixed
  definition, a `?` conditional, and `WHILE` open an indented suite with no
  `begin`/`end`, braces, or even a trailing colon — the indentation alone
  delimits the block.
- **Postfix expressions within a line**, like Forth: operands come before the
  operator, so `3 4 +` means `3 + 4`, and `N FACTORIAL` calls `FACTORIAL(N)`.
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

A program is a sequence of `!`-prefixed declarations. Execution begins at `MAIN`.

```
!FACTORIAL N
    N 1 <= ?
        1 @
    ELSE
        N  N 1 - FACTORIAL  *  @

!MAIN
    5 FACTORIAL PRINT     ; 120
```

### A historical character set

Iona deliberately stays within the characters available on a 1960s teletype
such as the Teletype Model 33: the 64-character set `0x20`–`0x5F`. Two
consequences shape the surface syntax:

- **Source is UPPERCASE.** That hardware had no lowercase letters at all.
  Identifiers are uppercase letters and digits (no underscore — its code point
  printed a left-arrow on a 1963 machine).
- **Terse operators.** Equality is `=` and not-equal is `<>` (ALGOL-style, and
  the `==`/`!=` convention is a 1972-era C-ism). Assignment is a single `!`.
  All are typeable on the machine.

### Lines are postfix token streams

Within a line, tokens are pushed onto an operand stack and operators consume
from it — exactly like Forth, but the stack is resolved at compile time, so the
generated code is ordinary efficient C with no runtime stack.

- `2 3 4 * +`  →  `2 + (3 * 4)`  →  `14`
- `N 1 -`  →  `N - 1`
- `A B MAX`  →  `MAX(A, B)`

### Declarations read prefix

Declarations are the one deliberate exception to postfix order, because they
name a thing rather than compute a value. A definition is marked by a `!`
prefixed to the name:

```
!NAME PARAM1 PARAM2
    <body>
```

The same `!` serves as assignment when *suffixed* to a name (`VALUE NAME!`); the
two never collide, because the definition `!` is line-initial while the
assignment `!` always trails its operands. All parameters and values are
integers in v0.

### Control flow reads postfix

The condition is evaluated first and the trailing marker consumes it — `?` for
a conditional, `WHILE` for a loop:

```
N 0 > ?
    "POSITIVE" PRINT
ELSE
    "NOT POSITIVE" PRINT

I N < WHILE
    I PRINT
    I 1 + I!
```

### Logical operators (conditions only)

`AND`, `OR`, and `NOT` combine conditions and **short-circuit**: `OR` stops at
the first true operand, `AND` at the first false one, and a guarded call on the
skipped side is never evaluated.

```
X 0 >  X 100 <  AND ?          ; 0 < X < 100
    "IN RANGE" PRINT

P 0 =  P VALID  OR ?           ; `P VALID` is not called when P = 0
    "OK" PRINT

DONE NOT WHILE
    STEP
```

They are control-flow words, not value-producing operators, so they are allowed
**only** in the condition of a `?` or `WHILE`. They do not double as bitwise
operators: a future bitwise set will get its own spelling (and since `| ^ ~`
are not on a 1960s teletype, word forms are the likely choice).

Under the hood a condition is compiled as *jumping code*: instead of computing a
`0`/`1` and testing it, each part branches straight to a true- or false-label.
That one mechanism yields both short-circuit evaluation and tight fused
compare-and-branch output, with no boolean ever materialized in a register.

### Assignment

Assignment is itself a postfix operator: push a value, then the target name with
`!` suffixed. Variables are declared automatically on first assignment.

```
5 X!           ; X = 5
X 1 + X!       ; X = X + 1
```

### Built-ins

| Form        | Meaning                                  |
|-------------|------------------------------------------|
| `+ - * / %` | integer arithmetic                       |
| `= <> < > <= >=` | comparisons (yield `0` / `1`)       |
| `AND OR NOT` | short-circuit logical operators (conditions only) |
| `VALUE NAME!` | assign `VALUE` into `NAME`              |
| `X PRINT`   | print an integer or string, then newline |
| `V @`       | set the function's return value to `V`    |
| `@`         | set the function's return value to `0`    |

### Return values and cleanup

Iona has no destructors, so cleanup code (closing files, freeing buffers) must
run explicitly. To make sure it always gets a chance, **`@` does not exit the
function** — it only *sets* the value to be returned. Execution continues to
the end of the function, which is the single point where it actually returns.
Anything after a `@` still runs:

```
!READSQUARED X
    "OPEN FILE" PRINT
    X X * @               ; set the result
    "CLOSE FILE" PRINT    ; still runs -- cleanup is never skipped

; prints: OPEN FILE / CLOSE FILE / then MAIN prints 25 for READSQUARED(5)
```

A consequence: because `@` no longer skips the rest of the function, use
`?`/`ELSE` (not a bare early `@`) when one branch must not run the other.
A function's return value defaults to `0` if `@` is never reached.

### Literals and comments

- Integer literals: `0`, `42`, `3628800`.
- String literals: `"HELLO"` (usable as a `PRINT` argument), with `\n`, `\t`,
  `\"`, `\\` escapes.
- Comments run from `;` to end of line.
- Indentation uses spaces; tabs are rejected.

## How it works

`ionac.py` is a single self-contained file with four stages:

1. **Tokenizer** — splits each physical line into postfix tokens.
2. **Line reader** — measures indentation and drops blank/comment lines.
3. **Block parser** — turns indentation into a nested AST of `!`-definitions,
   `?`-conditionals, `WHILE` loops, and statement nodes.
4. **Code generator** — lowers each postfix statement by walking the tokens
   with a *compile-time operand stack* of C expression strings. Function calls
   are materialized into temporaries so evaluation order is well defined.
   Conditions take a separate path: they are built into a small boolean tree
   and emitted as *jumping code* (branches to true/false labels) so that
   `AND`/`OR`/`NOT` short-circuit and compile to fused compare-and-branch.

## Status and roadmap

v0 is intentionally small but runs real recursive and iterative programs (see
`examples/` and `tests/`). Natural next steps:

- More types (`bool`, fixed-width ints, pointers, `char`/strings) with a real
  type checker rather than int-everywhere.
- `for` loops; bitwise operators as value operators, kept distinct from the
  short-circuit logical `AND`/`OR`/`NOT`. Their spelling is still open: the
  usual `| ^ ~` are not on a 1960s teletype, so word forms are the
  period-accurate choice.
- Struct / record types and manual memory (stack, arena, or `malloc`/`free`).
- Multiple return values and the stack-effect comments Forth is known for.
- A direct native backend (assembly or LLVM) to drop the C dependency.
