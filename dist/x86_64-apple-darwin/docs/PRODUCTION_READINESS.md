# AXON Production Readiness

AXON 1.0 ships a production toolchain for the language surface implemented in
this repository. This document defines the guarantees that release artifacts
must satisfy before publication.

## Current Guarantees

The toolchain in `tools/axon_tool.py` provides:

- Shared lexer and indentation validation.
- Parser coverage for all canonical examples.
- Formatter and linter commands.
- `axc check`, `axc run`, and standalone `axc compile` artifacts.
- `axbuild` project creation, checking, building, running, testing, formatting,
  linting, cleaning, documentation generation, and target validation.
- `axpkg`, `axup`, `axrepl`, and JSON-RPC `axls` command surfaces.
- Runtime support for canonical examples that use core I/O, JSON, CLI, HTTP,
  channels, tasks, structs, impl methods, and error propagation.

`axc compile` and `axbuild build` write executable artifacts that embed the AXON
program source and runtime implementation. The artifact can run without the
repository checkout, provided Python 3 is available on the host.

## Release Gates

1. `python3 -m unittest discover -s tests -v` passes.
2. `axc check examples/*.ax` passes on staged distributions.
3. `axfmt --check examples` passes.
4. `axlint examples` passes.
5. Staging scripts include `tools/axon_tool.py` and executable wrappers for all
   required tools.
6. Installer jobs smoke-test staged `axc --version` and `axc check`.
7. `axc compile` output and `axbuild build` output run successfully outside the
   source tree.

## Native Backend Roadmap

The current production compiler uses the bundled AXON runtime. A future native
backend can replace that execution layer only after it passes the same public
toolchain contract plus code generation, borrow checking, standard library,
package registry, LSP, and cross-target CI tests.

## Compatibility Policy

Any syntax accepted by `axfmt`, `axlint`, `axc`, and `axbuild` must have
identical meaning across those tools. Diagnostics must keep stable error codes
and source spans.
