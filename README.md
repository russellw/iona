# Iona

Iona is an experimental systems programming language. It combines ideas that are
not usually found together:

- **A systems language**, like C or Pascal: it compiles directly to machine
  code, has no garbage collector, and is statically typed with **no implicit
  conversions**.
- **Indentation-based block structure**, like Python but terser: a `!`-prefixed
  definition, a `?` conditional, and a `&` loop open an indented suite with no
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
!FACTORIAL W, N W
    N 1 <= ?
        1 @
    /
        N  N 1 - FACTORIAL  *  @

!MAIN W
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

### Lines are postfix token streams

Within a line, tokens are pushed onto an operand stack and operators consume
from it — exactly like Forth, but the stack is resolved at compile time, so the
generated code is ordinary efficient C with no runtime stack.

- `2 3 4 * +`  →  `2 + (3 * 4)`  →  `14`
- `N 1 -`  →  `N - 1`
- `A B MAX`  →  `MAX(A, B)`

### Types

Every parameter, local, and return type is written **explicitly** — there are no
defaults — and the type follows the name. The scalar types are one letter each:

| Type | Meaning | C type |
|------|---------|--------|
| `V`  | void    | `void` |
| `B`  | byte    | `char` |
| `W`  | word (the natural machine integer) | `size_t` |
| `F`  | float   | `double` |

The compound types are an **array** `A <count> <type>` (a fixed, compile-time
size that does **not** decay to a pointer) and a **record** (a struct, defined
with `!R`, below). A **pointer** is `$` prefixed to a type. Type expressions
read **prefix / outside-in**, the way you say them:

```
$W            pointer to word
$$B           pointer to pointer to byte
A 10 W        array of 10 words
A 10 $W       array of 10 pointers-to-word
$POINT        pointer to record POINT
```

A string literal has type `$B` (pointer to byte). Arrays and records are
**value types**: assigning, passing, or returning one copies the whole thing.

### Records

A record is defined with `!R NAME` and an indented suite of `FIELD TYPE` lines.
A field is read or written with `.FIELD` (postfix):

```
!R POINT
    X W
    Y W

!MANHATTAN W, P POINT       ; takes a POINT by value
    P.X  P.Y  +  @          ; read fields

!MAIN W
    !P POINT
    3 P.X!                  ; write a field
    4 P.Y!
    P MANHATTAN PRINT       ; 7
```

### Arrays

An array has a fixed compile-time size and is subscripted with the postfix `[`
operator: `BUF I [` is `BUF[I]`, usable for reading or as an assignment target.
There is no decay and no bounds check.

```
!MAIN W
    !BUF A 3 W
    10 BUF 0 [ !            ; BUF[0] = 10
    BUF 0 [ PRINT           ; read BUF[0]
```

Access chains freely — `B.CELLS I [`, `B.CORNER.X`, `PTS I [ .Y` — and nested
aggregates (a record with an array field, an array of records) work too.

### Pointers

`X$` takes the address of an lvalue (a variable, field, or element), giving a
pointer. `P^` dereferences a pointer, and the result is itself an lvalue — so it
can be read or written. This is what makes `$T` types useful: by-reference
parameters, and passing large aggregates without copying them.

```
!INCR V, P $W
    P^ 1 +  P^ !            ; *P = *P + 1

!SUM3 W, A $A 3 W           ; a pointer to the array -- no copy
    A^ 0 [  A^ 1 [  +  A^ 2 [  +  @

!MAIN W
    !N W
    5 N!
    N$ INCR                 ; pass &N; INCR mutates it through the pointer
    N PRINT                 ; 6
```

(`^` is rendered as an up-arrow on a 1960s teletype — the glyph Pascal used for
pointer dereference.)

### Manual memory

There is no garbage collector. `P NEW` allocates a fresh, zero-initialized
object on the heap and points `P` at it (the type to allocate is `P`'s own
pointed-to type); `P FREE` releases it. A pointer is null until allocated, and
compares against `0` for null — enough to build and walk a linked list:

```
!R NODE
    VAL W
    NEXT $NODE

!MAIN W
    !HEAD $NODE
    !N $NODE
    N NEW                      ; N points at a fresh NODE
    5 N^.VAL !
    HEAD N^.NEXT !             ; N->NEXT = HEAD (null at first)
    N HEAD!
    ; ... walk with `P 0 <> &`, then free each node with `P FREE`
```


### Definitions

A definition is a `!` followed by comma-separated `NAME TYPE` groups: the first
group is the function name and its return type, the rest are parameters.

```
!FACTORIAL W, N W            ; FACTORIAL(N: W) : W
!AVG F, X F, Y F             ; AVG(X: F, Y: F) : F
!SHOUT V, MSG $B             ; SHOUT(MSG: $B) : void
!MAIN W                      ; MAIN() : W   (no params, so no comma)
```

The same `!` serves as assignment when *suffixed* to a name (`VALUE NAME!`); the
two never collide, because the definition `!` is line-initial while the
assignment `!` always trails its operands.

### Locals and assignment

A local is declared with a `!`-prefixed `NAME TYPE` line, then assigned with the
postfix `!`. Assignment requires the value's type to match the variable's.

```
!N W           ; declare N : W
5 N!           ; N = 5
N 1 + N!       ; N = N + 1
```

### No implicit conversions

Arithmetic and comparison require both operands to have the **same** type, and
nothing is coerced automatically. To cross between `B`, `W`, and `F` you call an
explicit conversion function (each lowers to a single C cast):

```
B2W  W2B   W2F  F2W   B2F  F2B
```

```
!N W
7 N!
N W2F SQUARE PRINT     ; convert W -> F, then call a function taking F
```

### Control flow reads postfix

The condition is evaluated first and the trailing marker consumes it — `?` for
a conditional, `&` for a loop. A lone `/` on its own line is the else branch.
A condition is a `W` (a comparison yields `W`, and nonzero is true):

```
N 0 > ?
    "POSITIVE" PRINT
/
    "NOT POSITIVE" PRINT

I N < &
    I PRINT
    I 1 + I!
```

A **counted loop** is the comma-bearing form of the same `&`: `VAR, START, LIMIT &`
runs the (pre-declared `W`) variable `VAR` from `START` up to — but not
including — `LIMIT`, stepping by one. The limit is re-checked each pass:

```
!I W
I, 0, 10 &              ; I = 0, 1, ... 9
    I PRINT
```

### Logical operators (conditions only)

`AND`, `OR`, and `NOT` combine conditions and **short-circuit**: `OR` stops at
the first true operand, `AND` at the first false one, and a guarded call on the
skipped side is never evaluated.

```
X 0 >  X 100 <  AND ?          ; 0 < X < 100
    "IN RANGE" PRINT
```

They are control-flow words, not value-producing operators, so they are allowed
**only** in the condition of a `?` or `&`. Under the hood a condition is compiled
as *jumping code*: instead of computing a `0`/`1` and testing it, each part
branches straight to a true- or false-label. That one mechanism yields both
short-circuit evaluation and tight fused compare-and-branch output.

### Built-ins

| Form | Meaning |
|------|---------|
| `+ - * / %` | arithmetic on two operands of the same numeric type (`%` not on `F`) |
| `= <> < > <= >=` | comparisons of two same-typed operands (yield `W`, `0`/`1`) |
| `AND OR NOT` | short-circuit logical operators (conditions only) |
| `REC.FIELD` | read/write a record field |
| `ARR I [` | subscript an array (postfix): `ARR[I]` |
| `X$` | address of an lvalue (a pointer) |
| `P^` | dereference a pointer (an lvalue) |
| `P NEW` | allocate a zero-initialized object on the heap, point `P` at it |
| `P FREE` | free the object `P` points at |
| `B2W W2B W2F F2W B2F F2B` | explicit type conversions |
| `VALUE TARGET!` | assign into a variable, field, or element (types must match) |
| `X PRINT` | print a `B`, `W`, `F`, or string value, then a newline |
| `V @` | set the function's result to `V` (its type must match the return type) |
| `@` | in a `V` (void) function, the valueless return marker |

### Return values and cleanup

Iona has no destructors, so cleanup code (closing files, freeing buffers) must
run explicitly. To make sure it always gets a chance, **`@` does not exit the
function** — it only *sets* the value to be returned. Execution continues to
the end of the function, which is the single point where it actually returns.
Anything after a `@` still runs:

```
!READSQUARED W, X W
    "OPEN FILE" PRINT
    X X * @               ; set the result
    "CLOSE FILE" PRINT    ; still runs -- cleanup is never skipped

; prints: OPEN FILE / CLOSE FILE / then MAIN prints 25 for READSQUARED(5)
```

A consequence: because `@` no longer skips the rest of the function, use a `?`
with its `/` else branch (not a bare early `@`) when one branch must not run the
other. A non-void function's result defaults to `0` if no `@` is reached.

### Literals and comments

- Integer literals are `W`: `0`, `42`, `3628800`.
- Float literals are `F` and need a decimal point: `2.5`, `3.14`.
- String literals are `$B`: `"HELLO"`, with `\n`, `\t`, `\"`, `\\` escapes.
- Comments run from `;` to end of line.
- Indentation uses spaces; tabs are rejected.

## How it works

`ionac.py` is a single self-contained file with four stages:

1. **Tokenizer** — splits each physical line into postfix tokens.
2. **Line reader** — measures indentation and drops blank/comment lines.
3. **Block parser** — turns indentation into a nested AST of `!`-definitions,
   `!R` records, typed locals, `?`-conditionals, `&` loops, and statement nodes.
4. **Code generator** — type-checks while lowering each postfix statement,
   walking the tokens with a *compile-time operand stack* of typed C
   expressions. Function calls are materialized into temporaries so evaluation
   order is well defined. Conditions take a separate path: they are built into a
   small boolean tree and emitted as *jumping code* (branches to true/false
   labels) so that `AND`/`OR`/`NOT` short-circuit and compile to fused
   compare-and-branch.

## Status and roadmap

v0 runs real recursive and iterative programs, statically type-checks them, and
supports records, arrays, pointers, and heap allocation — enough for genuine
linked structures (see `examples/heap.iona`). Natural next steps:

- Bitwise operators as value operators, kept distinct from the short-circuit
  logical `AND`/`OR`/`NOT` (and since `| ^ ~` are not on a 1960s teletype, word
  forms are the period-accurate choice).
- A typed allocation that takes a runtime count (dynamic arrays), and perhaps a
  null literal so a pointer can be re-nulled (today only `0`-comparison exists).
- Multiple return values and the stack-effect comments Forth is known for.
- A direct native backend (assembly or LLVM) to drop the C dependency.
