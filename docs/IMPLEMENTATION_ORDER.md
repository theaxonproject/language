# AXON Implementation Order

The AXON project is built in the following order. Each phase must have tests
before the next phase starts.

1. Formal language specification: grammar, lexical structure, type system,
   ownership, control flow, modules, and diagnostics.
2. Stage 0 C11 lexer with indentation tokens and source spans.
3. Stage 0 C11 parser with recovery and AST spans.
4. AST pretty-printer for parser validation and formatter foundations.
5. Name resolver with module graph loading and circular import detection.
6. Core type checker without generics.
7. LLVM IR emitter for core language constructs.
8. Hello World native executable.
9. Generic type-checking and monomorphization.
10. Borrow checker with non-lexical lifetime analysis.
11. Core standard library: `core`, `str`, `collections`, `io`.
12. Canonical examples compiling and running.
13. Remaining standard library modules.
14. Self-hosted compiler in AXON and reproducible bootstrap verification.
15. Build system and package manager.
16. Language server.
17. Formatter, linter, and REPL.
18. Installers and toolchain manager.
19. Documentation site.
20. CI/CD pipeline.

## Completion Gate

A phase is complete only when:

- The implementation is covered by unit and integration tests.
- Diagnostics have stable error codes and source spans.
- The formatter can print all syntax introduced by the phase.
- No public behavior depends on undefined behavior or platform accidents.
- Cross-platform behavior is specified before implementation.
