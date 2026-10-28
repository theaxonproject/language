"""AXON compiler implementation.

A real compiler pipeline for the AXON programming language:

    source -> lexer -> parser -> AST -> semantic analysis
            -> (interpreter | C backend -> native executable)

The package is intentionally dependency-free (standard library only) so the
toolchain runs anywhere CPython 3.9+ is available. Native executables are
produced by emitting portable C11 and invoking the host C compiler, so the
resulting binaries are genuine machine code with no runtime interpreter
dependency.
"""

from __future__ import annotations

VERSION = "1.2.0"

__all__ = ["VERSION"]
