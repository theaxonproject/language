# AXON

Fast to write. Fast to run. Fast to ship.

AXON is a statically typed, compiled, general-purpose programming language with a
native toolchain: a real compiler (lexer → parser → semantic analyzer →
code generation), a tree-walking interpreter for fast iteration, a native
backend that emits genuine machine-code executables, a deterministic formatter,
a linter, a build system, a package-manager front end, a language server, and a
REPL.

This repository contains the AXON language specification **and** a working
production toolchain implementation in `tools/axonc/`.

## Architecture

The compiler is a clean, dependency-free pipeline (standard-library Python only):

| Stage | Module | Responsibility |
| --- | --- | --- |
| Lexer | `tools/axonc/lexer.py` | Character-level scanner with significant indentation (`INDENT`/`DEDENT`) and source spans |
| Parser | `tools/axonc/parser.py` | Recursive-descent parser producing a typed AST (`tools/axonc/ast.py`) |
| Analyzer | `tools/axonc/checker.py` | Name resolution, duplicate detection, struct-field validation, stable error codes |
| Type checker | `tools/axonc/typecheck.py` | Bidirectional local inference; type mismatches, arity, unknown fields/methods, match exhaustiveness, missing returns, mutability |
| Interpreter | `tools/axonc/interp.py` | Executes the full language for `axc run --interpret` and the REPL |
| Native backend | `tools/axonc/cbackend.py` + `runtime.c` | Lowers the compiled core to C11 and invokes the host C compiler to produce a native binary |

`axc compile` prefers the native backend (a real, standalone machine-code
executable). Programs that use language features the native backend does not yet
cover fall back to a portable self-contained artifact that runs through the
interpreter; the toolchain tells you which path was taken.

## Quick Start

```sh
# pick the distribution for your platform (or call tools/axon_tool.py directly)
AXC=dist/aarch64-unknown-linux-gnu/bin/axc

# type-check (catches type errors, missing match cases, missing returns, ...)
$AXC check examples/*.ax

# run the @test functions in a file (or every test in the current project)
$AXC test examples/13_testing.ax

# compile to a temporary native executable and run it
$AXC run examples/03_fibonacci.ax

# optimize before running (options go before the source file)
$AXC run --release examples/03_fibonacci.ax

# explicitly use the interpreter for features outside the native backend
$AXC run --interpret examples/03_fibonacci.ax

# compile to a native executable (requires a C compiler: cc/gcc/clang)
$AXC compile examples/03_fibonacci.ax -o fib
./fib                       # genuine ELF/Mach-O binary, no runtime dependency

# scaffold and build a project
dist/aarch64-unknown-linux-gnu/bin/axbuild new hello
cd hello
../dist/aarch64-unknown-linux-gnu/bin/axbuild run
```

`axc run [--release] FILE [args...]` requires a host C compiler and executes
native code. It preserves the current directory, standard input/output/error,
and the program's exit status, then removes the temporary executable. Arguments
after `FILE` are passed unchanged to the executable. The native backend does
not yet implement the `Cli` module; programs using it require `--interpret`.
Native compilation errors stop execution; `run` never silently falls back to
the interpreter. Use `axc run --interpret FILE [args...]` explicitly when needed.
`axbuild run/test` and the REPL continue to use the interpreter.

## Implemented language surface

Natively compiled today: functions and recursion, generic functions
(monomorphized per call site), closures (`x => ...`, `(a, b) => ...`,
`fn(x: int) -> int = ...`) and named functions as values, the scalar types
(`int`/`uint` family, `float`, `bool`, `str`), tuples and `let`/`for`/`match`
destructuring, structs with methods, traits with default methods (static
dispatch), enums — both C-style and with payloads (tagged unions), `match`
with literal/variant/tuple/or-patterns and guards, `if let`, `Option`/`Result`
with `try`/`?` propagation, `catch` handlers and the combinator set
(`map`/`and_then`/`map_err`/`unwrap_or_else`/...), `Vec` and `Map(str, V)`
collections (monomorphized, with higher-order methods such as
`map`/`filter`/`fold`/`sort_by`/`any`/`find`/`windows`/`chunks`), block-scoped
`defer`, optional chaining (`?.`), pipelines (`|>`), global constants, the
string method library, numeric methods and checked casts, file I/O,
`Process.args/env/exit`, `if`/`for`/`while`/`loop` control flow, inclusive
and exclusive ranges, f-strings, and the core I/O, assert, and panic
intrinsics. Integer arithmetic is overflow-checked (panics in debug builds,
wraps with `--release`). Compiled binaries and `axc run --interpret` produce
identical output — that parity is enforced by the test suite.

Available through the interpreter (and the portable artifact): the above plus
generic structs, structured-concurrency syntax, and the networked standard
library (JSON, HTTP client, CLI, TCP).

### Static checking

`axc check` type-checks a program before anything runs. For example:

```text
error[E112]: non-exhaustive match
   = non-exhaustive match: `Blue` not covered
  --> paint.ax:12:5
   = help: add the missing cases or a catch-all `case _ then ...` arm
```

It reports type mismatches (E110), wrong argument counts (E111), unknown
fields and methods (E103/E104), non-exhaustive matches (E112), invalid and
missing returns (E121/E123), mutation of immutable bindings (E122), `catch`
blocks that fall through (E124), stray `break`/`continue` (E125), and missing
trait methods (E126). `axlint` adds warnings for unused variables (W020) and
unreachable code (W021).

### Testing

```axon
@test
fn adds()
    assert_eq(1 + 1, 2)
```

`axc test` (or `axbuild test` in a project) runs every `@test` function and
reports each result; `@skip` marks a test as ignored.

See [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) for the exact
release gates and what is in-progress, and
[docs/spec/language-reference.md](docs/spec/language-reference.md) for the
normative language definition.

## Repository Layout

- `tools/axonc/` — the AXON compiler (lexer, parser, analyzer, interpreter, C backend)
- `tools/axon_tool.py` — multi-call toolchain entry point
- `docs/spec/` — language specification, grammar, and type-system contract
- `examples/` — canonical AXON programs (`01`–`10`, `12`, and `13` run today; `11` is the aspirational full-stdlib showcase)
- `tests/` — lexer, parser, checker, interpreter, native-backend, and command tests
- `dist/` — staged per-target distributions wrapping the shared implementation
- `packaging/` — installer and package metadata

## Testing

```sh
python3 -m unittest discover -s tests -v
```

## Version

AXON is versioned as `1.2.0` under the MIT License. See
[CHANGELOG.md](CHANGELOG.md) for what changed.
