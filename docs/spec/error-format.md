# AXON Diagnostic Format

Every diagnostic has:

- A stable code: `E001` through `E999` for errors, `W001` and above for
  warnings, `I001` and above for informational lints.
- A title.
- A primary source span.
- Optional secondary spans.
- A plain-language explanation.
- At least one concrete help message when a fix is known.
- A documentation link.

Example:

```text
error[E042]: cannot mutably borrow `counter` while it is shared
  --> src/main.ax:15:5
   |
12 |     let view = ref counter
   |                    ------- shared borrow starts here
15 |     counter = counter + 1
   |     ^^^^^^^ mutable borrow occurs here
16 |     println(view)
   |             ---- shared borrow later used here
   |
   = help: move the mutation after the last use of `view`
   = see: https://m5rcel.neocities.org/axon/errors/E042.xhtml
```

## Initial Diagnostic Catalog

The full catalog contains 200 compile-time diagnostics. Codes are grouped by
compiler phase so tooling can filter and present them predictably.

| Range | Phase |
| --- | --- |
| `E001`-`E049` | Lexing and source text |
| `E050`-`E099` | Parsing and grammar |
| `E100`-`E149` | Name resolution and modules |
| `E150`-`E249` | Type checking |
| `E250`-`E329` | Traits and generics |
| `E330`-`E399` | Pattern matching |
| `E400`-`E499` | Ownership and borrowing |
| `E500`-`E579` | Const evaluation |
| `E580`-`E649` | Unsafe and FFI |
| `E650`-`E749` | Code generation and linking |
| `E750`-`E849` | Packages and manifests |
| `E850`-`E929` | Toolchain integration |
| `E930`-`E999` | Reserved for future stable errors |

The catalog generator expands this table into per-code pages in the
documentation site during the documentation phase. The stable range assignment
is part of the language specification.
