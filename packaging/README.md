# AXON Packaging

Installer jobs package a staged AXON distribution from:

```text
dist/<target>/
    bin/
        axc
        axbuild
        axpkg
        axls
        axfmt
        axrepl
        axlint
        axup
    stdlib/
    docs/
```

Windows targets use `.exe` suffixes inside `bin`.

The packaging scripts verify that every required executable exists before they
build an installer. This keeps release failures early and clear: packaging does
not silently produce partial toolchains.

Artifacts are written to:

- `packaging/out/windows`
- `packaging/out/macos`
- `packaging/out/linux`
