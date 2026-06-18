# AXON Design Decisions

## Readable Blocks

AXON uses indentation and block-opening words because they make the nesting
structure visible without punctuation density. Blocks require both a keyword and
indentation so accidental indentation does not silently change meaning.

## Errors as Values

Exceptions hide control flow and allocation. AXON uses `T!E` result values so
fallibility appears in function signatures and propagates explicitly through
`try`.

## No Implicit Allocation

Operations that allocate are library calls or constructors. String interpolation
allocates a new string and is documented as such. Slices, borrows, and iterators
do not allocate.

## Hybrid Ownership

Move semantics and compile-time borrows provide predictable memory safety.
Reference counting is opt-in for data structures where single ownership is not
natural. This keeps common code deterministic while allowing practical graph and
callback patterns.

## Monomorphized Generics

Generic functions compile to specialized machine code. This avoids hidden
runtime dispatch and enables optimization across abstraction boundaries.

## Explicit Traits

Types implement traits explicitly. This prevents accidental API conformance and
makes public interfaces stable under refactoring.

## Toolchain as Language

The formatter, linter, package manager, build system, and language server share
one parser and one semantic model. A program accepted by one tool must be
understood identically by every other AXON tool.
