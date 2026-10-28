# Changelog

## 1.2.0

### Static type checking

`axc check` and every command that compiles or runs a program now type-check
the module first (`tools/axonc/typecheck.py`). New and newly-enforced
diagnostics:

| Code | Meaning |
| --- | --- |
| E110 | type mismatch (initializers, arguments, returns, operators, branches, list elements, assignments) |
| E111 | wrong number of arguments / unknown named argument |
| E103 / E104 | unknown field / unknown method, on user *and* builtin types |
| E112 | non-exhaustive `match` (enums, `Option`, `Result`, `bool`, and tuples of those) |
| E121 | invalid `return` (value in a `void` function, missing value) |
| E122 | assignment to, or mutation of, an immutable binding |
| E123 | function can reach its end without returning a value |
| E124 | `catch` block falls through |
| E125 | `break` / `continue` outside a loop |
| E126 | `impl ... as Trait` is missing a required method |

### Language

- Closures with several parameters use parentheses: `(a, b) => a + b`
  (also `() => ...`). A bare `x => ...` closure takes exactly one parameter,
  which removes the `f(a, b => c)` ambiguity.
- Inclusive ranges: `for i in 1..=10 do`.
- Tuple destructuring: `let (name, age) = pair`, `for (k, v) in map do`,
  `for (i, x) in v.enumerate() do`.
- String literals inside f-string interpolations: `f"{name.unwrap_or("anon")}"`.
- `defer` is block-scoped, as the reference specifies, and runs on `return`,
  `break`, `continue`, and `try` propagation.
- Trait default methods are inherited by every implementing type (previously
  they were not callable in either engine), and trait signatures without a
  body are required methods.
- `try` inside a closure returns from the closure.
- `a?.method()` produces an Option, like `a?.field`.

### Native backend

Now compiled natively instead of falling back to the interpreter: closures
and function values, generic functions (monomorphized per call site),
tuples, `defer`, optional chaining, pipelines, the higher-order `Vec` methods,
the Option/Result combinators, `Map` iteration and `entries()`, `for` over
`str`, `Process.args/env/exit`, and `if`/`and`/`or` operands that need
setup statements.

Correctness fixes in the emitted C:

- Arguments and operands are evaluated left to right (C leaves the order
  unspecified; GCC evaluated `f(a(), b())` right to left).
- User identifiers that collide with C keywords or libc names (`double`,
  `floor`, `exit`, ...) are renamed instead of breaking the C build.
- Integer arithmetic is overflow-checked in debug builds (panic) and wraps in
  `--release` builds (well-defined, via unsigned arithmetic); float-to-int
  casts are range-checked.
- Floats print exactly like the interpreter (`100.0`, not `1e+02`; `nan`,
  `inf`).
- Float `%` uses `fmod` (it previously failed to compile).
- Panics flush standard output first, so output ordering matches.

### Standard library (both engines)

- `Vec`: `insert`, `remove`, `clear`, `extend`, `index_of`, `take`, `skip`,
  `dedup`, `enumerate`, `zip`, `windows`, `chunks`, `min`, `max`, `fold`,
  `any`, `all`, `count`, `find`, `position`, `for_each`, `iter`, `clone`,
  `get_mut` (completing the contract's required set).
- `Option`: `and_then`, `filter`, `or`, `ok_or`; `Result`: `map_err`,
  `and_then`, `unwrap_or_else`, `expect`, `err`.
- `str`: `find`, `strip_prefix`, `strip_suffix`, `split_once`, `bytes`
  natively; lengths and slices are UTF-8 byte based in both engines.
- `int`: `is_even`, `is_odd`, `signum`; `float`: `pow`, `is_nan`.
- `Map`: `clear`, `entries`/`iter`.
- `Process.args()`, `Process.env(name)`, `Process.exit(code)`.

### Interpreter

- Debug-mode integer semantics matching native code: 64-bit overflow panics,
  C-style `/` and `%`, `round()` half away from zero.
- Deep recursion (tens of thousands of frames) works; runaway recursion is a
  `stack overflow` panic instead of a Python traceback.
- Out-of-range indexing, missing map keys, and similar faults are AXON panics
  rather than host-language crashes.
- `type_name` returns AXON type names.

### Tooling

- `axc test [paths]` and `axbuild test` run `@test` functions with per-test
  results and a summary; `@skip` marks a test as ignored. New projects from
  `axbuild new` include `@test` examples.
- `axrepl` keeps `let` bindings between inputs, accepts `fn`/`struct`/`enum`/
  `impl` definitions and multi-line blocks, and renders diagnostics.
- `axlint` warns about unused variables (W020) and unreachable code (W021).

### Examples

- `examples/12_inventory.ax`: closures, tuples, generics, combinators, trait
  defaults, and `defer`, compiled natively.
- `examples/13_testing.ax`: unit tests with `@test`.
