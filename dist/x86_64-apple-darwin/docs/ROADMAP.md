# AXON Roadmap / Continuation Notes

This file is the working to-do list for finishing AXON. It records exactly
where the implementation stands after the 1.2.0 work and what to pick up next,
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

## State as of 1.2.0

- **Static type checker** (`tools/axonc/typecheck.py`, roadmap item 6):
  bidirectional local inference over the whole module, with E110–E112 and
  E121–E126 diagnostics (mismatches, arity, unknown fields/methods,
  Maranget-style match exhaustiveness, missing returns, immutability, `catch`
  fall-through, stray loop control, missing trait methods). Unknown library
  types and generic parameters are treated leniently (never a false
  positive). All examples and tests type-check cleanly.
- **Native closures** (item 1): closure values are `{env, fn}` pairs with a
  heap-allocated capture struct; named functions become values through
  trampolines. Unlocks `Vec.map/filter/fold/sort_by/any/all/count/find/
  position/for_each` and the Option/Result combinators natively.
- **Native generics** (item 4, functions): per-call-site monomorphization,
  including type parameters bound by closure return types.
- **Native tuples, `defer`, `?.`, `|>`** (item 5); `let`/`for`/`match`
  tuple destructuring; `for` over `Map` and `str`.
- **Process API** (part of item 3): `Process.args/env/exit` in both engines.
- **Semantics fixes found while closing parity gaps**: left-to-right
  evaluation in emitted C (C leaves argument order unspecified), checked
  integer arithmetic (debug panics, `--release` wraps), C-style `%`,
  half-away-from-zero `round`, byte-based `str` lengths/slices, identical
  float formatting, C-reserved identifiers (`double`, `floor`, ...) renamed in
  the C output, block-scoped `defer`, `try` inside closures, trait default
  methods inherited by implementing types, deep recursion in the interpreter.
- **Language additions**: `(a, b) => body` closures, `a..=b`, `let (a, b) =`,
  string literals inside f-string interpolations.
- **Tooling**: `axc test` / `axbuild test` run `@test` functions (`@skip`
  supported); `axrepl` keeps bindings and accepts definitions; `axlint` warns
  about unused variables (W020) and unreachable code (W021).

## Next up (priority order)

1. **Native JSON.** Port `Json.parse`/`Json.stringify`/`parse_as(Struct)` to
   `runtime.c` with a tagged JSON value type; this makes examples 05 and 10
   compile natively. Example 10 also references an undeclared `ConfigError`
   type, which should become a declared enum.
2. **Native CLI.** `App`/`CliMatches` (example 07) on top of the now-native
   `Process.args()`.
3. **Generic structs and enums** natively (the function monomorphizer's
   `_tenv`/`_specs` machinery generalizes), plus `Map` keys other than `str`.
4. **Type checker, phase 2.** Unification variables for empty collections
   (`let v = []` currently infers `Vec(_)`), generic call-site inference so
   `identity(5)` types as `int`, and trait-bound checking.
5. **Native TCP/HTTP** (examples 04/08) via BSD sockets/winsock in
   `runtime.c`, behind the same `Result` ABI.
6. **Tasks/channels** natively (threads + a mutex-protected queue).
7. **Closures capturing mutable variables** natively (box captured `mut`
   locals) — today they report E800 and run with `--interpret`.
8. **Reference semantics audit.** Native structs are passed by value while the
   interpreter shares references; today `mut self` methods paper over most of
   it. Decide and enforce one semantics in both engines.
9. **Memory:** the native runtime never frees (fine for short programs);
   add an arena or RC when long-running servers land (after item 5).
10. **Self-hosting** (`docs/IMPLEMENTATION_ORDER.md` phase 14) and an LLVM
    backend remain the long-term goals; the C backend is the stepping stone.

## Release checklist

1. `python3 -m unittest discover -s tests` green.
2. Bump `VERSION` in `tools/axonc/__init__.py` and `version` in `axon.toml`.
3. Restage: `for t in x86_64-unknown-linux-gnu x86_64-unknown-linux-musl
   aarch64-unknown-linux-gnu aarch64-unknown-linux-musl x86_64-apple-darwin
   aarch64-apple-darwin; do ./packaging/stage-dist.sh <version> $t; done`
4. Commit, push, tag `v<version>`, push the tag — the release workflow
   publishes the archives.
