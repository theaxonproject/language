# AXON 1.0 Language Reference

AXON is a compiled, statically typed systems language designed around readable
syntax, explicit effects, value-based errors, deterministic resource cleanup,
and a first-class toolchain.

This document is normative. Implementations that disagree with this reference
are not AXON 1.0 implementations.

## 1. Lexical Structure

AXON source files are UTF-8 text and use the extension `.ax`.

Compilation treats each file as one module. A module name is derived from the
file stem unless overridden by the package manifest.

### Newlines and Indentation

Statements are newline-delimited. Blocks are introduced by a block-opening
keyword and delimited by indentation. One indentation level is exactly four
spaces. Tabs are invalid in indentation.

The lexer emits `INDENT` when indentation increases and `DEDENT` when it
decreases. A physical line ending inside parentheses, brackets, or call
argument lists does not end a statement. A trailing backslash continues an
expression onto the next physical line.

### Comments

Single-line comments start with `##` and continue to the end of the line.
Block comments start with `###` and end at the next `###`. Block comments do
not nest.

### Identifiers

Identifiers use Unicode XID rules with AXON naming restrictions:

- Values and functions: `snake_case`
- Types, traits, enum variants, and modules: `PascalCase`
- Constants: `SCREAMING_SNAKE_CASE`

The compiler validates naming during parsing for declarations and during linting
for imported aliases and generated symbols.

### Keywords

Reserved keywords are:

`and`, `as`, `break`, `capture`, `case`, `catch`, `const`, `continue`, `defer`,
`do`, `dyn`, `else`, `enum`, `extern`, `false`, `fn`, `for`, `if`, `impl`, `in`,
`is`, `let`, `loop`, `match`, `mut`, `never`, `none`, `not`, `or`, `ref`,
`return`, `select`, `self`, `struct`, `task`, `task_group`, `then`, `trait`,
`true`, `try`, `type`, `unsafe`, `use`, `void`, `where`, `while`, `with`.

### Literals

Integer literals support decimal, binary, octal, and hexadecimal forms:

- `42`
- `0b1010`
- `0o755`
- `0xFF`

Underscores may separate digits but not lead, trail, or appear consecutively.

Float literals are IEEE 754 decimal literals such as `3.14`, `10.0`, and
`6.02e23`.

String forms are:

- `"text"` for escaped UTF-8 strings
- `r"raw \n text"` for raw strings
- `f"Hello {name}"` for interpolation
- `"""multi line"""` for heredoc strings

Character literals use single quotes and must contain one Unicode scalar value.

## 2. Operators

The following table lists operators from highest to lowest precedence.

| Level | Operators | Associativity | Overloadable |
| --- | --- | --- | --- |
| 15 | `()`, `[]`, `.`, `?.`, `.0` | left | method names only |
| 14 | unary `+`, `-`, `not`, `~`, `*`, `&`, `&mut` | right | arithmetic unary only |
| 13 | `*`, `/`, `%` | left | yes |
| 12 | `+`, `-` | left | yes |
| 11 | `<<`, `>>` | left | yes |
| 10 | `&` | left | yes |
| 9 | `^` | left | yes |
| 8 | `|` | left | yes |
| 7 | `==`, `!=`, `<`, `<=`, `>`, `>=`, `is`, `is not` | non-assoc | selected traits |
| 6 | `and` | left | no |
| 5 | `or` | left | no |
| 4 | `|>` | left | no |
| 3 | `try`, `try catch`, `try or` | right | no |
| 2 | `if then else`, `match` | right | no |
| 1 | lambda `fn(...) = ...`, `x => ...` | right | no |

Assignment operators are statements, not expressions.

`->` is a return-type delimiter, not an operator. It is retained because AXON's
required toolchain and examples use it. It cannot be overloaded.

## 3. Types

### Primitive Types

| Type | Size | Default | Overflow |
| --- | ---: | --- | --- |
| `int8` | 1 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `int16` | 2 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `int32` | 4 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `int64` | 8 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `uint8` | 1 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `uint16` | 2 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `uint32` | 4 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `uint64` | 8 | `0` | checked in debug, wrapping in release unless checked APIs are used |
| `int` | 8 | `0` | same as `int64` on supported targets |
| `uint` | 8 | `0` | same as `uint64` on supported targets |
| `float32` | 4 | `0.0` | IEEE 754 |
| `float64` | 8 | `0.0` | IEEE 754 |
| `bool` | 1 | `false` | not applicable |
| `byte` | 1 | `0` | alias for `uint8` |
| `rune` | 4 | `0` | alias for Unicode scalar `uint32` |
| `str` | 16 | `""` | immutable UTF-8 fat pointer |
| `void` | 0 | none | return type only |
| `never` | 0 | none | non-returning expressions |

`int` and `uint` are 64-bit on all AXON 1.0 targets. This avoids source-level
behavior changing between 64-bit targets and future 32-bit targets.

### Structs

Structs are named-field product types.

```axon
struct Point
    x: float64
    y: float64 = 0.0
```

Field order is declaration order. The default layout is AXON layout: stable
within one compiler version, optimized for alignment, and not C-compatible.
Use `@repr(C)` or `@c_struct` for C layout.

Fields are immutable unless the binding or receiver is mutable.

### Enums

Enums are tagged unions with optional variant payloads.

```axon
enum ParseState
    Start
    Number(value: int)
    Done
```

The compiler chooses the smallest tag that can represent every variant. Payloads
use normal AXON layout unless the enum is annotated with a representation.

### Tuples

Tuples are fixed-size heterogeneous values. Fields are accessed by numeric field
projection: `pair.0`, `pair.1`.

### Arrays and Slices

`[T; N]` is an owned fixed-size array. `[T]` is a non-owning slice fat pointer
containing pointer and length. Arrays coerce to slices when borrowed.

Indexing is checked in debug mode and unchecked in release mode unless the
package profile enables release bounds checks.

### Optional

`T?` is either `Some(T)` or `none`. Optional values must be explicitly handled
with `match`, `try or`, or optional chaining.

### Result

`T!E` is either a success value of type `T` or an error value of type `E`.
Errors are ordinary values. `try` unwraps success or returns the error from the
enclosing function.

## 4. Type Inference

AXON uses bidirectional local type inference.

1. Declarations with annotations push expected types into their initializers.
2. Initializers without annotations synthesize types bottom-up.
3. Function calls infer generic arguments from actual arguments first, then from
   the expected return type.
4. Single-expression functions may infer return type from the body.
5. Multi-statement functions require explicit return type unless they return
   `void`.
6. Recursive functions require an explicit return type and explicit parameter
   types. The body is checked under that signature.
7. Numeric literals start as type variables constrained by literal value and
   expected type. If unconstrained, integer literals default to `int` and float
   literals default to `float64`.
8. Trait constraints are solved after unification. Ambiguous trait selection is
   a compile error.

The implementation uses union-find unification variables, an occurs check, and
generalization only at `let` bindings that do not capture mutable references.

## 5. Ownership and Memory

AXON has deterministic destruction and no garbage collector.

Values have one owner by default. Assignment, argument passing, and returning
move ownership unless the value implements `Copy`. Primitive scalar values,
immutable references, and raw pointers are `Copy`.

`clone()` performs an explicit deep copy through the `Clone` trait.

`ref T` is a shared immutable borrow. `mut ref T` is an exclusive mutable borrow.
The borrow checker enforces:

- Any number of shared borrows may exist at once.
- A mutable borrow excludes all other borrows.
- A borrowed value cannot be moved until the borrow ends.
- A reference cannot outlive the value it references.

Borrowing is checked on the control-flow graph with non-lexical lifetimes. A
borrow ends at its last use, not necessarily at the end of the lexical block.

Heap shared ownership is explicit through `Rc(T)` for single-threaded graphs and
`Arc(T)` for cross-thread sharing. Cycles require `Weak(T)` or explicit teardown.

## 6. Expressions

Most control-flow constructs are expressions. The type of an `if` expression is
the common type of all branches. A `match` expression must be exhaustive and all
arms must have a common type.

`never` coerces to any expected type.

Pipeline `value |> f(arg)` is lowered to `f(value, arg)`. When the right side is
a method call, `value |> .method(arg)` is lowered to `value.method(arg)`.

Optional chaining `a?.b` evaluates to `none` if `a` is `none`, otherwise projects
`b` from the unwrapped value.

## 7. Statements

Statements include declarations, assignments, returns, loop control, `defer`,
expression statements, and compound control flow.

`defer expr` registers an expression to run when the enclosing scope exits.
Defers run in last-in, first-out order on normal return, error propagation, and
panic unwinding.

## 8. Functions and Closures

Functions are declared with `fn`.

```axon
fn add(a: int, b: int) -> int
    return a + b
```

Default parameters are evaluated at the call site. Variadic parameters produce
a slice inside the function.

Closures capture by reference by default. Explicit capture copies or moves the
listed values into the closure according to normal ownership rules.

```axon
let add_one = fn(x: int) -> int = x + 1
let twice = x => x * 2
let owned = capture(name) fn() -> str = name
```

## 9. Structs, Traits, and Impl Blocks

Methods are defined in `impl` blocks. `self` is immutable unless written
`mut self`.

Traits define required methods, associated types, and default methods.

```axon
trait Printable
    fn print(self) -> str

impl Point as Printable
    fn print(self) -> str
        return f"Point({self.x}, {self.y})"
```

Trait satisfaction is explicit. There is no structural trait matching.

Generic functions with trait bounds use static dispatch and monomorphization.
`dyn Trait` uses runtime vtable dispatch.

## 10. Modules and Packages

Each `.ax` file is a module. Imports use `use`.

```axon
use Core
use Http.{ HttpClient, Request }
```

The resolver builds the full module graph before type checking. Circular imports
are rejected unless the cycle consists only of type signatures with no value
initialization dependency.

## 11. Concurrency

`task do` starts a lightweight task on the M:N scheduler. Values moved into a
task must implement `Send`. Shared references across tasks require `Sync`.

`task_group do` provides structured concurrency: all child tasks complete before
the group exits, and errors cancel siblings unless explicitly handled.

Channels are typed and may be buffered or unbuffered. `select do` waits for the
first ready channel operation or timeout arm.

## 12. Pattern Matching

Patterns include literals, bindings, `_`, tuples, structs, enum variants,
ranges, nested patterns, or-patterns, and guards.

Match exhaustiveness is checked with a decision-tree algorithm. Non-exhaustive
matches are compile errors. Unreachable arms are warnings.

## 13. Error Handling

There are no exceptions. Fallible functions return `T!E`.

```axon
let data = try File.open(path).read_all()
```

`try expr catch err then fallback` handles an error inline.
`try expr or fallback` unwraps with a fallback value.

## 14. Unsafe AXON

Unsafe operations require `unsafe do` or `unsafe fn`.

Raw pointer types are `*T` and `*mut T`. Unsafe permits raw dereference, pointer
arithmetic, unchecked indexing, and calls to unsafe functions.

Unsafe code is not exempt from type checking. It only relaxes memory-safety
checks that cannot be proven statically.

## 15. C FFI

`@extern_c` binds C functions. `@export_c` exports AXON functions with a C ABI.
`@repr(C)` and `@c_struct` produce C-compatible layout.

Primitive type mappings:

| AXON | C |
| --- | --- |
| `int8` | `int8_t` |
| `uint8` / `byte` | `uint8_t` |
| `int16` | `int16_t` |
| `uint16` | `uint16_t` |
| `int32` | `int32_t` |
| `uint32` / `rune` | `uint32_t` |
| `int64` / `int` | `int64_t` |
| `uint64` / `uint` | `uint64_t` |
| `float32` | `float` |
| `float64` | `double` |
| `bool` | `_Bool` |
| `str` | `axon_str` ABI struct |

AXON owned values may not cross the FFI boundary unless represented by an
explicit ABI type.
