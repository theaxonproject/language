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
| Interpreter | `tools/axonc/interp.py` | Executes the full language for `axc run` and the REPL |
| Native backend | `tools/axonc/cbackend.py` + `runtime.c` | Lowers the compiled core to C11 and invokes the host C compiler to produce a native binary |

`axc compile` prefers the native backend (a real, standalone machine-code
executable). Programs that use language features the native backend does not yet
cover fall back to a portable self-contained artifact that runs through the
interpreter; the toolchain tells you which path was taken.

## Quick Start

```sh
# pick the distribution for your platform (or call tools/axon_tool.py directly)
AXC=dist/aarch64-unknown-linux-gnu/bin/axc

# type-check
$AXC check examples/*.ax

# run through the interpreter
$AXC run examples/03_fibonacci.ax

# compile to a native executable (requires a C compiler: cc/gcc/clang)
$AXC compile examples/03_fibonacci.ax -o fib
./fib                       # genuine ELF/Mach-O binary, no runtime dependency

# scaffold and build a project
dist/aarch64-unknown-linux-gnu/bin/axbuild new hello
cd hello
../dist/aarch64-unknown-linux-gnu/bin/axbuild run
```

## Implemented language surface

Natively compiled today: functions and recursion, the scalar types
(`int`/`uint` family, `float`, `bool`, `str`), structs with methods and trait
methods (static dispatch), enums — both C-style and with payloads (tagged
unions), `match` with literal/variant/or-patterns and guards, `if let`,
`Option`/`Result` with `try`/`?` propagation and `catch` handlers, `Vec` and
`Map(str, V)` collections (monomorphized, with the full method set), global
constants, the string method library (`split`/`trim`/`contains`/`replace`/
`parse`/slicing/...), numeric methods and casts, file I/O,
`if`/`for`/`while`/`loop` control flow, `and`/`or`/`not`,
arithmetic/comparison/bitwise operators, f-strings, and the core I/O, assert,
and panic intrinsics. Compiled binaries and `axc run` produce identical output
— that parity is enforced by the test suite.

Available through the interpreter (and the portable artifact): the above plus
closures, optional chaining, defer, structured-concurrency syntax, and the
networked standard library (JSON, HTTP client, CLI, TCP).

See [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) for the exact
release gates and what is in-progress, and
[docs/spec/language-reference.md](docs/spec/language-reference.md) for the
normative language definition.

## Repository Layout

- `tools/axonc/` — the AXON compiler (lexer, parser, analyzer, interpreter, C backend)
- `tools/axon_tool.py` — multi-call toolchain entry point
- `docs/spec/` — language specification, grammar, and type-system contract
- `examples/` — canonical AXON programs (`01`–`10` run today; `11` is the aspirational full-stdlib showcase)
- `tests/` — lexer, parser, checker, interpreter, native-backend, and command tests
- `dist/` — staged per-target distributions wrapping the shared implementation
- `packaging/` — installer and package metadata

## Testing

```sh
python3 -m unittest discover -s tests -v
```

## Version

AXON is versioned as `1.0.0` under the MIT License.
