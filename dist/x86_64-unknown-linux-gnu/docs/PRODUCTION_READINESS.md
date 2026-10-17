# AXON Production Readiness

AXON 1.0 ships a working compiler toolchain. This document defines what the
implementation guarantees and the gates release artifacts must satisfy.

## Implementation

The compiler in `tools/axonc/` is a real, staged pipeline:

- **Lexer** (`lexer.py`): character-level scanner with significant indentation
  (`INDENT`/`DEDENT` tokens), full literal/operator/keyword coverage, source
  spans, and line-continuation rules (brackets, trailing `\`, leading-`.`
  method chains).
- **Parser** (`parser.py`): recursive-descent parser producing a typed AST
  (`ast.py`), using precedence climbing for expressions.
- **Semantic analyzer** (`checker.py`): name resolution (unknown-symbol
  detection), duplicate-definition detection, struct-field validation, with
  stable error codes and precise spans.
- **Interpreter** (`interp.py` + `stdlib.py`): a tree-walking evaluator that
  executes the full implemented language surface, used by `axc run --interpret`, `axbuild
  run/test`, and the REPL.
- **Native backend** (`cbackend.py` + `runtime.c`): lowers the statically-typed
  core of the AST to portable C11 and invokes the host C compiler to produce a
  genuine machine-code executable with no interpreter dependency.

## Toolchain surface

- `axc check`, `axc run` (native execution), `axc run --interpret` (explicit
  interpreter execution), `axc compile` (native, with portable fallback).
- `axbuild new/check/build/run/test/fmt/lint/clean/doc/cross`.
- `axfmt` (idempotent formatter), `axlint` (style + semantic diagnostics).
- `axpkg`, `axup`, `axls` (JSON-RPC language server), `axrepl`.

## Native vs portable artifacts

`axc run [--release] FILE [args...]` compiles to a temporary native executable,
runs it with the caller's working directory and standard streams, propagates
its exit status, and cleans up the executable. All semantic and compilation
errors stop execution. Unsupported native features report E800 and suggest
`axc run --interpret FILE [args...]`; there is no automatic fallback in `run`.
Options precede FILE; subsequent arguments are forwarded to the executable.

`axc compile` and `axbuild build` first attempt native compilation through the C
backend. If the program uses a construct the native backend does not yet cover,
the toolchain emits a portable self-contained artifact (bundling the compiler
and running through the interpreter) and reports on stderr which path it took.
This keeps `compile` total while the native backend's coverage grows.

## Release Gates

1. `python3 -m unittest discover -s tests` passes.
2. `axc check examples/01..10` passes on staged distributions.
3. `axfmt --check examples` passes (idempotent).
4. `axlint examples/01..10` passes.
5. Staging (`packaging/stage-dist.sh`) includes `tools/axon_tool.py`, the full
   `tools/axonc/` package, and executable wrappers for every tool.
6. `axc compile examples/01_hello.ax` produces a binary whose magic bytes are
   ELF or Mach-O (a real native executable), and it runs outside the source
   tree.
7. The native fibonacci and struct/trait examples compile and run.

## Native backend coverage

Compiled natively today: functions/recursion, scalar types (`int`/`uint`
family, `float`, `bool`, `str`), structs with methods and trait methods (static
dispatch), enums (C-style and tagged unions with payloads), `match` lowering
(literal/variant/wildcard/or-patterns, bindings, guards, runtime
non-exhaustiveness panic), `if let`, the `Option`/`Result` ABI with `try`/`?`
propagation and inline/block `catch` handlers, monomorphized `Vec(T)` and
`Map(str, V)` with their method sets, global constants, the string method
library, numeric methods/casts, file I/O (`File.open/read/write/exists`),
`if`/`for`-range/`for`-over-Vec/`while`/`loop`, `and`/`or`/`not`,
arithmetic/comparison/bitwise operators, f-strings (with recursive value
formatting for structs, enums, options, results, vectors, and maps), and core
I/O + assert + panic intrinsics.

Native/interpreter parity is enforced by `tests/test_native_backend.py`, which
compiles each feature program natively AND runs it under the interpreter,
asserting byte-identical output.

In progress for the native backend (works in the interpreter today): closures
and higher-order Vec methods (`map`/`filter`/`sort_by`), user generics and
monomorphization, tuples, `defer`, optional chaining (`?.`), tasks/channels,
and the networked/JSON standard library. Each lands behind the same public
toolchain contract: stable error codes, stable spans, and identical observable
behavior between `axc run --interpret` and a compiled binary. See `docs/ROADMAP.md` for
the full plan.

## Compatibility Policy

Any syntax accepted by `axfmt`, `axlint`, `axc`, and `axbuild` has identical
meaning across those tools. Diagnostics keep stable error codes (see
`tools/axonc/errors.py`) and source spans.
