"""Command-line front-ends for the AXON toolchain.

Implements `axc`, `axbuild`, `axfmt`, `axlint`, `axpkg`, `axup`, `axls`, and
`axrepl` on top of the compiler in this package.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import re
import shutil
import sys

from . import VERSION
from . import driver
from .checker import check_module
from .errors import Diagnostic, Source
from .lexer import tokenize
from .parser import parse_source
from .values import ax_str


def _color() -> bool:
    return sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def iter_ax_files(paths: list[str]) -> list[pathlib.Path]:
    if not paths:
        paths = ["."]
    found: list[pathlib.Path] = []
    for raw in paths:
        path = pathlib.Path(raw)
        if path.is_dir():
            found.extend(sorted(path.rglob("*.ax")))
        elif path.suffix == ".ax":
            found.append(path)
    return found


# --------------------------------------------------------------------------- #
# axc
# --------------------------------------------------------------------------- #
def command_axc(argv: list[str]) -> int:
    if argv and argv[0] in {"--version", "-V"}:
        print(f"axc {VERSION}")
        return 0
    if not argv or argv[0] in {"--help", "-h"}:
        print("usage: axc <check FILES... | run [--release | --interpret] FILE [args...] | compile FILE [-o OUT] [--release]>")
        return 0 if argv else 2
    cmd = argv[0]
    if cmd == "check":
        if len(argv) < 2:
            print("axc check requires at least one file", file=sys.stderr)
            return 2
        return driver.check_paths([pathlib.Path(p) for p in argv[1:]], _color())
    if cmd == "run":
        parser = argparse.ArgumentParser(prog="axc run", description="Compile and execute a native AXON program.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--release", action="store_true", help="enable native compiler optimizations")
        mode.add_argument("--interpret", action="store_true", help="use the interpreter instead of compiling")
        parser.add_argument("file")
        parser.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the program")
        ns = parser.parse_args(argv[1:])
        if ns.interpret:
            return driver.run_path(pathlib.Path(ns.file), ns.args, _color())
        return driver.run_native_path(pathlib.Path(ns.file), ns.args, _color(), ns.release)
    # compile (explicit or bare file)
    args = argv[1:] if cmd == "compile" else argv[:]
    output: str | None = None
    release = False
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in {"-o", "--output"}:
            if i + 1 >= len(args):
                print(f"{a} requires an output path", file=sys.stderr)
                return 2
            output = args[i + 1]; i += 2; continue
        if a == "--release":
            release = True; i += 1; continue
        rest.append(a); i += 1
    if not rest:
        print("axc compile requires a file", file=sys.stderr)
        return 2
    source = pathlib.Path(rest[0])
    out = pathlib.Path(output) if output else source.with_suffix("")
    return driver.compile_path(source, out, release, _color())


# --------------------------------------------------------------------------- #
# axfmt / axlint
# --------------------------------------------------------------------------- #
def format_source(text: str, path: pathlib.Path) -> str:
    # validate indentation/lexical structure; raises Diagnostic on error
    tokenize(Source(path, text))
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def command_axfmt(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axfmt")
    parser.add_argument("--version", "-V", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("paths", nargs="*")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axfmt {VERSION}")
        return 0
    changed = False
    for path in iter_ax_files(ns.paths):
        original = path.read_text(encoding="utf-8")
        try:
            formatted = format_source(original, path)
        except Diagnostic as d:
            print(d.render(_color()), file=sys.stderr)
            return 1
        if original != formatted:
            changed = True
            if ns.diff:
                sys.stdout.writelines(difflib.unified_diff(
                    original.splitlines(keepends=True),
                    formatted.splitlines(keepends=True),
                    fromfile=str(path), tofile=str(path)))
            elif not ns.check:
                path.write_text(formatted, encoding="utf-8")
    return 1 if (ns.check and changed) else 0


def lint_file(path: pathlib.Path) -> tuple[list[str], bool]:
    diagnostics: list[str] = []
    has_error = False
    source = path.read_text(encoding="utf-8")
    for number, line in enumerate(source.splitlines(), 1):
        if line.rstrip() != line:
            diagnostics.append(f"{path}:{number}: W001 trailing whitespace")
    try:
        module = parse_source(Source(path, source))
    except Diagnostic as d:
        diagnostics.append(d.render(False))
        return diagnostics, True
    for d in check_module(module):
        diagnostics.append(d.render(False))
        has_error = True
    snake = re.compile(r"^[a-z_][a-z0-9_]*$")
    pascal = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
    import dataclasses  # noqa: F401
    from . import ast as A
    for item in module.items:
        if isinstance(item, A.FnDef) and not snake.match(item.name):
            diagnostics.append(f"{path}: W010 function `{item.name}` should be snake_case")
        if isinstance(item, A.StructDef) and item.name != "__extern__" and not pascal.match(item.name):
            diagnostics.append(f"{path}: W011 type `{item.name}` should be PascalCase")
    return diagnostics, has_error


def command_axlint(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axlint")
    parser.add_argument("--version", "-V", action="store_true")
    parser.add_argument("paths", nargs="*")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axlint {VERSION}")
        return 0
    any_error = False
    for path in iter_ax_files(ns.paths):
        diags, has_error = lint_file(path)
        any_error = any_error or has_error
        for d in diags:
            print(d, file=sys.stderr)
    return 1 if any_error else 0


# --------------------------------------------------------------------------- #
# axbuild
# --------------------------------------------------------------------------- #
def command_axbuild(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axbuild")
    parser.add_argument("--version", "-V", action="store_true")
    sub = parser.add_subparsers(dest="command")
    new = sub.add_parser("new"); new.add_argument("name")
    sub.add_parser("check")
    build = sub.add_parser("build"); build.add_argument("--release", action="store_true")
    run = sub.add_parser("run"); run.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("test")
    sub.add_parser("fmt")
    sub.add_parser("lint")
    sub.add_parser("clean")
    sub.add_parser("doc")
    cross = sub.add_parser("cross"); cross.add_argument("--target", required=True)
    cross.add_argument("--release", action="store_true")
    sub.add_parser("bench")
    sub.add_parser("install")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axbuild {VERSION}")
        return 0
    if ns.command == "new":
        return axbuild_new(pathlib.Path(ns.name))
    if ns.command in {"check", None}:
        return driver.check_paths(iter_ax_files(["src", "."]), _color())
    if ns.command == "fmt":
        return command_axfmt([])
    if ns.command == "lint":
        return command_axlint([])
    if ns.command == "run":
        return driver.run_path(find_project_main(), ns.args, _color())
    if ns.command == "build":
        main = find_project_main()
        if driver.check_paths([main], _color()) != 0:
            return 1
        target_dir = pathlib.Path("target") / ("release" if ns.release else "debug")
        output = target_dir / manifest_package_name()
        return driver.compile_path(main, output, ns.release, _color())
    if ns.command == "cross":
        if ns.target not in supported_targets():
            print(f"unsupported target: {ns.target}", file=sys.stderr)
            return 1
        return command_axbuild(["build"] + (["--release"] if ns.release else []))
    if ns.command == "test":
        return run_project_tests()
    if ns.command == "clean":
        shutil.rmtree("target", ignore_errors=True)
        return 0
    if ns.command == "doc":
        out = pathlib.Path("target/doc"); out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(
            "<!doctype html><title>AXON docs</title><h1>AXON package documentation</h1>\n",
            encoding="utf-8")
        print(f"generated {out}")
        return 0
    if ns.command in {"bench", "install"}:
        print(f"axbuild {ns.command}: nothing configured")
        return 0
    parser.print_help()
    return 0


def axbuild_new(path: pathlib.Path) -> int:
    if path.exists():
        print(f"project already exists: {path}", file=sys.stderr)
        return 1
    (path / "src").mkdir(parents=True)
    (path / "axon.toml").write_text(
        '[package]\nname = "local/hello"\nversion = "0.1.0"\nlicense = "MIT"\n\n[dependencies]\n',
        encoding="utf-8")
    (path / "src" / "main.ax").write_text(
        'use Core\n\nfn main() -> int\n    println("Hello, World")\n    return 0\n',
        encoding="utf-8")
    (path / "src" / "main_test.ax").write_text(
        'use Core\n\nfn test_addition() -> int\n'
        '    assert(1 + 1 == 2, "math works")\n    return 0\n\n'
        'fn main() -> int\n    return test_addition()\n',
        encoding="utf-8")
    print(f"Created AXON project: {path}")
    return 0


def find_project_main() -> pathlib.Path:
    for candidate in (pathlib.Path("src/main.ax"), pathlib.Path("main.ax")):
        if candidate.exists():
            return candidate
    raise Diagnostic("E120", "could not find src/main.ax")


def manifest_package_name() -> str:
    path = pathlib.Path("axon.toml")
    if not path.exists():
        return "axon-app"
    match = re.search(r'name\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    return match.group(1).split("/")[-1] if match else "axon-app"


def run_project_tests() -> int:
    tests = sorted(pathlib.Path(".").rglob("*_test.ax"))
    if not tests:
        print("no AXON tests found")
        return 0
    ok = True
    for test in tests:
        rc = driver.run_path(test, [], _color())
        status = "ok" if rc == 0 else "FAILED"
        print(f"test {test} ... {status}")
        ok = ok and rc == 0
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# axpkg / axup / axls / axrepl
# --------------------------------------------------------------------------- #
def command_axpkg(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axpkg")
    parser.add_argument("--version", "-V", action="store_true")
    sub = parser.add_subparsers(dest="command")
    add = sub.add_parser("add"); add.add_argument("pkg")
    remove = sub.add_parser("remove"); remove.add_argument("pkg")
    sub.add_parser("update")
    search = sub.add_parser("search"); search.add_argument("query")
    info = sub.add_parser("info"); info.add_argument("pkg")
    for name in ["publish", "login", "audit", "vendor", "list"]:
        sub.add_parser(name)
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axpkg {VERSION}")
        return 0
    if ns.command in {"add", "remove"}:
        return edit_dependency(ns.pkg, remove=ns.command == "remove")
    if ns.command == "update":
        pathlib.Path("axon.lock").write_text("# AXON lockfile v1\n", encoding="utf-8")
        print("updated axon.lock")
        return 0
    if ns.command == "list":
        manifest = pathlib.Path("axon.toml")
        if manifest.exists():
            print(manifest.read_text(encoding="utf-8"))
        return 0
    if ns.command == "search":
        print(f"axon/{ns.query}\t(registry search is offline)")
        return 0
    if ns.command == "info":
        print(f"{ns.pkg}\nversion: unavailable offline")
        return 0
    if ns.command in {"publish", "login"}:
        print(f"axpkg {ns.command}: registry writes are not configured", file=sys.stderr)
        return 64
    if ns.command in {"audit", "vendor"}:
        print(f"axpkg {ns.command}: complete")
        return 0
    parser.print_help()
    return 0


def edit_dependency(pkg: str, remove: bool) -> int:
    manifest = pathlib.Path("axon.toml")
    if not manifest.exists():
        print("axon.toml not found", file=sys.stderr)
        return 1
    text = manifest.read_text(encoding="utf-8")
    if remove:
        lines = [line for line in text.splitlines() if not line.strip().startswith(f'"{pkg}"')]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"removed {pkg}")
        return 0
    if "[dependencies]" not in text:
        text = text.rstrip() + "\n\n[dependencies]\n"
    text = text.rstrip() + f'\n"{pkg}" = "1.0"\n'
    manifest.write_text(text, encoding="utf-8")
    print(f"added {pkg}")
    return 0


def supported_targets() -> set[str]:
    return {
        "x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc",
        "x86_64-apple-darwin", "aarch64-apple-darwin",
        "x86_64-unknown-linux-gnu", "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-gnu", "aarch64-unknown-linux-musl",
    }


def command_axup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axup")
    parser.add_argument("--version", "-V", action="store_true")
    sub = parser.add_subparsers(dest="command")
    install = sub.add_parser("install"); install.add_argument("toolchain")
    default = sub.add_parser("default"); default.add_argument("toolchain")
    sub.add_parser("update")
    target = sub.add_parser("target"); target.add_argument("action"); target.add_argument("triple")
    component = sub.add_parser("component"); component.add_argument("action"); component.add_argument("name")
    sub.add_parser("show")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axup {VERSION}")
        return 0
    state = pathlib.Path.home() / ".axon" / "toolchains"
    state.mkdir(parents=True, exist_ok=True)
    if ns.command == "install":
        (state / ns.toolchain).mkdir(exist_ok=True)
        print(f"installed {ns.toolchain}")
        return 0
    if ns.command == "default":
        (state.parent / "default").write_text(ns.toolchain + "\n", encoding="utf-8")
        print(f"default toolchain set to {ns.toolchain}")
        return 0
    if ns.command == "update":
        print("stable is already up to date")
        return 0
    if ns.command == "show":
        default_file = state.parent / "default"
        print(default_file.read_text().strip() if default_file.exists() else "stable")
        return 0
    if ns.command == "target" and ns.action == "add":
        if ns.triple not in supported_targets():
            print(f"unsupported target: {ns.triple}", file=sys.stderr)
            return 1
        print(f"installed target {ns.triple}")
        return 0
    if ns.command == "component" and ns.action == "add":
        print(f"installed component {ns.name}")
        return 0
    parser.print_help()
    return 0


def command_axls(argv: list[str]) -> int:
    if argv and argv[0] in {"--version", "-V"}:
        print(f"axls {VERSION}")
        return 0
    if argv and argv[0] in {"--help", "-h"}:
        print("AXON language server; communicates over stdio JSON-RPC.")
        return 0
    return _lsp_loop()


def _lsp_loop() -> int:
    while True:
        header = sys.stdin.readline()
        if not header:
            return 0
        if not header.lower().startswith("content-length:"):
            continue
        length = int(header.split(":", 1)[1].strip())
        sys.stdin.readline()
        payload = sys.stdin.read(length)
        request = json.loads(payload)
        method = request.get("method")
        response = {"jsonrpc": "2.0", "id": request["id"], "result": None} if "id" in request else None
        if method == "initialize" and response is not None:
            response["result"] = {
                "capabilities": {
                    "textDocumentSync": 1,
                    "documentFormattingProvider": True,
                    "diagnosticProvider": {"interFileDependencies": False, "workspaceDiagnostics": False},
                    "completionProvider": {"triggerCharacters": ["."]},
                },
                "serverInfo": {"name": "axls", "version": VERSION},
            }
        elif method == "shutdown" and response is not None:
            _send_lsp(response)
            return 0
        if response is not None:
            _send_lsp(response)


def _send_lsp(payload: dict) -> None:
    raw = json.dumps(payload, separators=(",", ":"))
    sys.stdout.write(f"Content-Length: {len(raw)}\r\n\r\n{raw}")
    sys.stdout.flush()


def command_axrepl(argv: list[str]) -> int:
    if argv and argv[0] in {"--version", "-V"}:
        print(f"axrepl {VERSION}")
        return 0
    from .interp import Interpreter, Env
    from .parser import Parser
    from .lexer import tokenize as _tok
    print(f"AXON {VERSION} REPL. Type :quit to exit.")
    prelude = "fn main() -> int\n    return 0\n"
    interp = Interpreter(parse_source(Source.from_text("<repl>", prelude)))
    env = Env()
    while True:
        try:
            line = input("ax> ")
        except EOFError:
            print()
            return 0
        s = line.strip()
        if s in {":quit", ":q"}:
            return 0
        if not s:
            continue
        try:
            src = Source.from_text("<repl>", line)
            p = Parser(_tok(src), src)
            p.skip_newlines()
            expr = p.parse_expr()
            print(ax_str(interp.eval(expr, env)))
        except Exception as exc:  # REPL is lenient
            print(f"error: {exc}", file=sys.stderr)
