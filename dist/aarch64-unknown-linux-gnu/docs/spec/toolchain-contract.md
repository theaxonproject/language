# AXON Toolchain Contract

All AXON tools use the same lexer, parser, resolver, type checker, and package
graph model. A source file accepted by one tool must have identical syntax and
semantic meaning in every other tool.

## axbuild

`axbuild` is the project build system.

Commands:

| Command | Behavior |
| --- | --- |
| `axbuild build` | Build debug profile. |
| `axbuild build --release` | Build release profile with profile-defined optimization. |
| `axbuild run [args]` | Build and run the package binary. |
| `axbuild test` | Build and run all tests. |
| `axbuild test --coverage` | Run tests and emit coverage reports. |
| `axbuild bench` | Run benchmarks. |
| `axbuild clean` | Remove build artifacts. |
| `axbuild check` | Parse, resolve, borrow-check, and type-check without codegen. |
| `axbuild doc` | Generate HTML documentation. |
| `axbuild fmt` | Format all source files. |
| `axbuild lint` | Run lints. |
| `axbuild cross --target <triple>` | Cross-compile for a target triple. |
| `axbuild install` | Install the package binary to the configured path. |

### Manifest Schema

`axon.toml` is the package manifest.

```toml
[package]
name = "author/package"
version = "1.0.0"
description = "Short package description"
authors = ["Name <email@example.com>"]
license = "MIT"
homepage = "https://example.com"
repository = "https://example.com/repo.git"
keywords = ["cli", "json"]
categories = ["command-line-utilities"]

[dependencies]
"axon/http" = "1.0"

[dev_dependencies]
"axon/test_extra" = "1.0"

[build_dependencies]
"axon/bindgen_hooks" = "1.0"

[features]
default = ["tls"]
tls = []

[profile.debug]
opt_level = 0
debug_info = true
overflow_checks = true
assertions = true
lto = false

[profile.release]
opt_level = 3
debug_info = false
overflow_checks = false
assertions = false
lto = true

[targets.x86_64-unknown-linux-musl]
static_link = true
```

The lockfile records exact package versions, content hashes, registry URLs,
author signing keys, and resolved feature sets.

## axpkg

`axpkg` manages dependencies and registry publishing.

Commands:

| Command | Behavior |
| --- | --- |
| `axpkg add <pkg>` | Add dependency and resolve lockfile. |
| `axpkg remove <pkg>` | Remove dependency and resolve lockfile. |
| `axpkg update` | Update all dependencies within semver constraints. |
| `axpkg update <pkg>` | Update one dependency. |
| `axpkg publish` | Sign and publish the current package. |
| `axpkg login` | Authenticate with registry. |
| `axpkg search <query>` | Search package registry. |
| `axpkg info <pkg>` | Display package metadata. |
| `axpkg audit` | Check dependency vulnerability advisories. |
| `axpkg vendor` | Copy dependencies into `vendor/`. |

Version resolution uses PubGrub. Packages are addressed as `author/name`.
Published packages are signed with Ed25519. Lockfile hashes use SHA-256 and
include normalized archive contents.

### Registry API

Base URL: `https://m5rcel.neocities.org/axon/api/v1`.

`GET /packages/{name}`

```json
{
  "name": "axon/json_extra",
  "description": "JSON helpers",
  "latest": "1.2.0",
  "versions": ["1.0.0", "1.1.0", "1.2.0"],
  "owners": ["axon"],
  "license": "MIT",
  "repository": "https://github.com/axon-lang/json_extra"
}
```

`GET /packages/{name}/{version}`

```json
{
  "name": "axon/json_extra",
  "version": "1.2.0",
  "manifest": {},
  "sha256": "hex-encoded-content-hash",
  "signature": "hex-encoded-ed25519-signature",
  "signing_key": "hex-encoded-public-key",
  "published_at": "2026-05-20T00:00:00Z"
}
```

`GET /packages/{name}/{version}/download` returns a normalized tar archive.

`POST /packages/publish` accepts multipart package archive, manifest, signature,
and signing-key proof. Authentication uses bearer tokens scoped to package
owners.

`GET /search?q=<query>` returns ranked package search results.

## axfmt

`axfmt` is deterministic and zero-config.

- Indentation is four spaces.
- Maximum line length is 100.
- Imports are sorted stdlib, third-party, local.
- Multiline collections and calls use trailing commas.
- Formatting is idempotent.
- Formatting fails on syntax errors.

`axfmt --check` exits nonzero when files would change.
`axfmt --diff` prints unified diffs without writing.

## axlint

Lints have stable IDs, severity, a bad example, and a good example.

Mandatory categories are correctness, style, performance, and security. Lints
that require type information run after full semantic analysis. Lints that only
need syntax may run incrementally in the language server.

## axls

`axls` implements LSP 3.17:

- Lifecycle: initialize, initialized, shutdown, exit.
- Synchronization: didOpen, didChange, didClose, didSave.
- Diagnostics from parser, resolver, type checker, borrow checker, and linter.
- Completion, hover, signature help, definitions, references, rename, formatting,
  code actions, inlay hints, semantic tokens, call hierarchy, document symbols,
  and workspace symbols.

The server maintains an incremental parse tree per open document and invalidates
semantic regions through dependency edges in the module graph.

## axrepl

The REPL compiles snippets into an in-memory session module. Definitions persist
until `:reset`. Special commands are:

- `:type expr`
- `:doc symbol`
- `:load file.ax`
- `:reset`
- `:quit`

## axup

`axup` installs and manages AXON toolchains:

- `axup install stable`
- `axup install 1.0.0`
- `axup install nightly`
- `axup default 1.0.0`
- `axup update`
- `axup target add <triple>`
- `axup component add axls`

## Supported Targets

AXON 1.0 supports:

- `x86_64-pc-windows-msvc`
- `aarch64-pc-windows-msvc`
- `x86_64-apple-darwin`
- `aarch64-apple-darwin`
- `x86_64-unknown-linux-gnu`
- `x86_64-unknown-linux-musl`
- `aarch64-unknown-linux-gnu`
- `aarch64-unknown-linux-musl`

`wasm32-unknown-unknown` is reserved as a future target and is not part of
AXON 1.0 release certification.

## Installer Deliverables

Release artifacts contain `axc`, `axbuild`, `axpkg`, `axls`, `axfmt`, `axrepl`,
`axlint`, `axup`, standard library sources, target sysroots, and documentation.

Windows packages include WiX MSI, WinGet manifest, and NSIS installer.
macOS packages include signed and notarized `.pkg`, Homebrew formula, and
`axup` shell installer. Linux packages include `.deb`, `.rpm`, `.tar.gz`, AUR
source and binary package recipes, AppImage, Snap, and Flatpak manifests.
