"""Compilation driver: ties together parsing, checking, the interpreter, and the
native C backend, and produces executable artifacts.

`axc compile` / `axbuild build` prefer the native backend (genuine machine-code
binary). Programs that use language features the native backend does not yet
cover fall back to a portable self-contained artifact that bundles the compiler
and runs the program through the interpreter.
"""

from __future__ import annotations

import base64
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from . import ast
from .cbackend import generate_c
from .checker import check_module
from .errors import Diagnostic, Source
from .interp import Interpreter
from .parser import parse_source
from .values import AxPanic


def load_module(path: pathlib.Path) -> ast.Module:
    return parse_source(Source.from_path(path))


def check_paths(paths: list[pathlib.Path], color: bool = False) -> int:
    ok = True
    for path in paths:
        try:
            module = load_module(path)
            diagnostics = check_module(module)
            for d in diagnostics:
                print(d.render(color), file=sys.stderr)
                print(file=sys.stderr)
            if diagnostics:
                ok = False
        except Diagnostic as d:
            print(d.render(color), file=sys.stderr)
            print(file=sys.stderr)
            ok = False
    return 0 if ok else 1


def run_path(path: pathlib.Path, args: list[str], color: bool = False) -> int:
    try:
        module = load_module(path)
        diagnostics = check_module(module)
        fatal = [d for d in diagnostics if d.code.startswith("E1") or d.code == "E100"]
        for d in diagnostics:
            print(d.render(color), file=sys.stderr)
        if fatal:
            return 1
        old_argv = sys.argv[:]
        sys.argv = ["axon", str(path), *args]
        try:
            return Interpreter(module, args).run_main()
        finally:
            sys.argv = old_argv
    except Diagnostic as d:
        print(d.render(color), file=sys.stderr)
        return 1
    except AxPanic as exc:
        print(f"axon: panic: {exc.message}", file=sys.stderr)
        return 101


def find_c_compiler() -> str:
    """Locate the host C compiler: $CC, then cc/gcc/clang, then MSVC's cl."""
    env_cc = os.environ.get("CC")
    if env_cc:
        return env_cc
    for candidate in ("cc", "gcc", "clang", "cl"):
        if shutil.which(candidate):
            return candidate
    return "cc"


def native_output_path(output: pathlib.Path) -> pathlib.Path:
    """Native executables need an `.exe` suffix on Windows."""
    if os.name == "nt" and output.suffix.lower() != ".exe":
        return output.with_suffix(output.suffix + ".exe")
    return output


def compile_native(module: ast.Module, source_path: pathlib.Path,
                   output: pathlib.Path, release: bool) -> pathlib.Path:
    """Compile to a native executable via the C backend. Raises Diagnostic on
    unsupported constructs (E800) or C-compiler failure (E810). Returns the
    path of the produced executable (which gains `.exe` on Windows)."""
    c_source = generate_c(module)
    cc = find_c_compiler()
    output = native_output_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    is_msvc = pathlib.Path(cc).stem.lower() == "cl"
    with tempfile.TemporaryDirectory(prefix="axon-cc-") as tmp:
        c_file = pathlib.Path(tmp) / (source_path.stem + ".c")
        c_file.write_text(c_source, encoding="utf-8")
        if is_msvc:
            opt = "/O2" if release else "/Od"
            cmd = [cc, "/nologo", "/std:c11", opt, str(c_file),
                   f"/Fe:{output}", f"/Fo:{tmp}\\"]
        else:
            flags = ["-O2", "-flto"] if release else ["-O0", "-g"]
            cmd = [cc, "-std=c11", *flags, "-o", str(output), str(c_file), "-lm"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr.strip() + "\n" + result.stdout.strip()).strip()
            raise Diagnostic(
                "E810",
                f"the C compiler failed:\n{detail}",
                None,
                "this usually indicates an internal backend bug; please report it",
            )
    if os.name != "nt":
        output.chmod(0o755)
    return output


def _package_files() -> dict[str, str]:
    pkg = pathlib.Path(__file__).parent
    files: dict[str, str] = {}
    for child in sorted(pkg.iterdir()):
        if child.suffix in {".py", ".c"}:
            files[child.name] = base64.b64encode(child.read_bytes()).decode("ascii")
    return files


def emit_portable(source_path: pathlib.Path, output: pathlib.Path) -> None:
    """Write a self-contained artifact that bundles the compiler and runs the
    program through the interpreter. Requires python3 on the host."""
    files = _package_files()
    source_b64 = base64.b64encode(source_path.read_bytes()).decode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    launcher = f"""#!/usr/bin/env python3
import base64, pathlib, sys, tempfile
FILES = {files!r}
SOURCE = {source_b64!r}
NAME = {source_path.name!r}
_dir = tempfile.mkdtemp(prefix="axon-app-")
_pkg = pathlib.Path(_dir) / "axonc"
_pkg.mkdir()
for _name, _data in FILES.items():
    (_pkg / _name).write_bytes(base64.b64decode(_data))
sys.path.insert(0, _dir)
from axonc.parser import parse_source
from axonc.errors import Source
from axonc.interp import Interpreter
from axonc.values import AxPanic
_src = pathlib.Path(_dir) / NAME
_src.write_bytes(base64.b64decode(SOURCE))
sys.argv = [str(output_name)] + sys.argv[1:]
try:
    raise SystemExit(Interpreter(parse_source(Source.from_path(_src)), sys.argv[1:]).run_main())
except AxPanic as exc:
    print("axon: panic: " + exc.message, file=sys.stderr)
    raise SystemExit(101)
""".replace("output_name", repr(output.name))
    output.write_text(launcher, encoding="utf-8")
    output.chmod(0o755)


def compile_path(source_path: pathlib.Path, output: pathlib.Path,
                 release: bool = False, color: bool = False) -> int:
    try:
        module = load_module(source_path)
    except Diagnostic as d:
        print(d.render(color), file=sys.stderr)
        return 1
    diagnostics = check_module(module)
    for d in diagnostics:
        print(d.render(color), file=sys.stderr)
    if diagnostics:
        return 1
    try:
        produced = compile_native(module, source_path, output, release)
        print(f"compiled {produced} (native)")
        return 0
    except Diagnostic as d:
        if d.code == "E800":
            emit_portable(source_path, output)
            print(
                f"compiled {output} (portable) — uses features outside the "
                f"native core:\n  {d.message}",
                file=sys.stderr,
            )
            return 0
        print(d.render(color), file=sys.stderr)
        return 1
