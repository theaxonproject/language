# AXON Roadmap / Continuation Notes

This file is the working to-do list for finishing AXON. It records exactly
where the implementation stands after the 1.1.0 work and what to pick up next,
in priority order. Update it whenever a line item lands.

## State as of 1.1.0

Done and covered by tests (`python3 -m unittest discover -s tests`):

- Full pipeline: lexer (INDENT/DEDENT) → parser → checker → interpreter, plus
  a native C11 backend (`tools/axonc/cbackend.py` + `runtime.c`) with a
  portable-artifact fallback for anything the native core doesn't cover yet.
- Native backend now compiles: consts, enums (incl. tagged-union payloads),
  `match` (literals, variants, or-patterns, bindings, guards), `if let`,
  `Option`/`Result` with `try`/`?`/`catch`, monomorphized `Vec(T)` and
  `Map(str, V)`, the str/int/float method library, file I/O, `and`/`or`/`not`,
  f-string formatting for all of the above. Interpreter/native parity is
  asserted byte-for-byte in `tests/test_native_backend.py`.
- Parser fixes: `and`/`or` as binary operators, inline `let x = try E catch e
  then EXPR`, bounded `try` operand so `try f() or fallback` works.
- Interpreter fixes: `try` propagation keeps the `Result`/`Option` wrapper,
  enum constructors accept named payload arguments, lowercase scalar type
  names usable as values (`s.parse(int)`), `File.read_to_string` returns
  `Err` instead of crashing, `File.exists` added.
- Windows: `.exe` output suffix, compiler discovery (cc/gcc/clang/cl), MSVC
  invocation flags, platform-aware tests, `stage-dist.ps1` now ships the
  compiler package (it previously omitted `tools/axonc` entirely).
- CI (`.github/workflows/ci.yml`): tests on Linux/macOS/Windows.
  Release (`.github/workflows/release.yml`): pushing a `v*` tag tests on all
  three OSes, stages all distribution targets, and publishes a GitHub Release
  with the archives. **To cut a release: bump `VERSION` in
  `tools/axonc/__init__.py` + `axon.toml`, restage `dist/`, then push a tag.**

## Next up (priority order)

1. **Closures in the native backend.** Lower `x => body` to a function pointer
   plus a capture struct; then unlock `Vec.map/filter/sort_by` natively.
   The interpreter semantics to match live in `interp.py` (`AxClosure`).
2. **Native JSON.** Port `Json.parse`/`Json.stringify`/`parse_as(Struct)` to
   `runtime.c` with a tagged JSON value type; this makes examples 05 and 10
   compile natively. Example 10 also needs user-declared error types for
   `Config!ConfigError` (today an unknown error type falls back to portable).
3. **Native CLI + process API.** `Process.args()`, `App`/`CliMatches` (example
   07). Wire `argc/argv` through `main` (the C `main` already receives them).
4. **Generics + monomorphization** for user functions/structs (the container
   monomorphizer in `cbackend.py` — `ensure()`/tag machinery — is the pattern
   to generalize).
5. **Tuples, `defer`, optional chaining (`?.`)** in the native backend.
6. **Real type checker.** `checker.py` is name-resolution only; add a typing
   pass (reuse the backend's tag inference, which already type-checks the
   native subset as a side effect) so `axc check` catches type errors without
   compiling.
7. **Native TCP/HTTP** (examples 04/08) via BSD sockets/winsock in
   `runtime.c`, behind the same `Result` ABI.
8. **Tasks/channels** natively (threads + a mutex-protected queue).
9. **Reference semantics audit.** Native structs are passed by value while the
   interpreter shares references; today `mut self` methods paper over most of
   it. Decide and enforce one semantics in both engines.
10. **Vec method receivers must be lvalues** natively (`f().push(x)` is a C
    error today, E810 instead of a clean E800 diagnostic).
11. **Memory:** the native runtime never frees (fine for short programs);
    add an arena or RC when long-running servers land (after item 7).
12. **Self-hosting** (`docs/IMPLEMENTATION_ORDER.md` phase 14) and an LLVM
    backend remain the long-term goals; the C backend is the stepping stone.

## Release checklist

1. `python3 -m unittest discover -s tests` green.
2. Bump `VERSION` in `tools/axonc/__init__.py` and `version` in `axon.toml`.
3. Restage: `for t in x86_64-unknown-linux-gnu x86_64-unknown-linux-musl
   aarch64-unknown-linux-gnu aarch64-unknown-linux-musl x86_64-apple-darwin
   aarch64-apple-darwin; do ./packaging/stage-dist.sh <version> $t; done`
4. Commit, push, tag `v<version>`, push the tag — the release workflow
   publishes the archives.
