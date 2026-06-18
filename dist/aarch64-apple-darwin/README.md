# AXON

Fast to write. Fast to run. Fast to ship.

AXON is a statically typed, compiled, general-purpose programming language with a
native toolchain, deterministic formatter, package manager, language server,
build system, and cross-platform standard library.

This repository contains the AXON language specification and a runnable
production toolchain implementation. The implementation order is tracked in
[docs/IMPLEMENTATION_ORDER.md](docs/IMPLEMENTATION_ORDER.md), and release gates
are tracked in [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md).

## Quick Start

```sh
dist/x86_64-apple-darwin/bin/axc check examples/*.ax
dist/x86_64-apple-darwin/bin/axc run examples/01_hello.ax
dist/x86_64-apple-darwin/bin/axbuild new hello
cd hello
../dist/x86_64-apple-darwin/bin/axbuild run
```

## Repository Layout

- `docs/spec/` - language specification, grammar, and type-system contract
- `examples/` - canonical runnable AXON programs
- `tools/` - shared AXON production toolchain implementation
- `tests/` - lexer, parser, formatter, runtime, and command tests
- `dist/` - staged target distributions that wrap the shared implementation
- `site/` - static documentation website
- `packaging/` - installer and package metadata

## Version

AXON is versioned as `1.0.0` under the MIT License. The current compiler emits
self-contained executable artifacts for the implemented language surface.
