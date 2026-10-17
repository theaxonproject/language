"""End-to-end and unit tests for the AXON toolchain.

Exercises the real compiler pipeline: lexer (INDENT/DEDENT), parser, semantic
analyzer, interpreter, and the native C backend that produces machine-code
executables.
"""

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
AXON_TOOL = TOOLS / "axon_tool.py"
EXAMPLES = ROOT / "examples"

sys.path.insert(0, str(TOOLS))


def _load(modname, relpath):
    spec = importlib.util.spec_from_file_location(modname, TOOLS / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


# import the package modules directly for unit tests
from axonc.errors import Source  # noqa: E402
from axonc.lexer import tokenize  # noqa: E402
from axonc.parser import parse_source, parse_path  # noqa: E402
from axonc.checker import check_module  # noqa: E402
from axonc.interp import Interpreter  # noqa: E402
from axonc.cbackend import generate_c  # noqa: E402

HAVE_CC = any(shutil.which(cc) for cc in ("cc", "gcc", "clang", "cl"))

# native executables' magic bytes: ELF (Linux), Mach-O (macOS), PE (Windows)
NATIVE_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"MZ\x90\x00", b"MZ")


def native_path(path: pathlib.Path) -> pathlib.Path:
    """The executable path `axc compile -o path` actually produces."""
    if os.name == "nt" and path.suffix.lower() != ".exe":
        return path.with_suffix(path.suffix + ".exe")
    return path


def is_native_binary(path: pathlib.Path) -> bool:
    head = path.read_bytes()[:4]
    return any(head.startswith(m) for m in NATIVE_MAGIC)

# canonical examples that compile natively today
NATIVE_EXAMPLES = ["01_hello", "02_fizzbuzz", "03_fibonacci", "09_struct_trait"]
# every canonical example except the aspirational full-stdlib showcase
CORE_EXAMPLES = [
    "01_hello", "02_fizzbuzz", "03_fibonacci", "04_http_get", "05_json_file",
    "06_concurrent_tasks", "07_cli_tool", "08_tcp_echo_server",
    "09_struct_trait", "10_error_chain",
]


def axon(tool, *args, cwd=ROOT, env=None):
    return subprocess.run(
        [sys.executable, str(AXON_TOOL), tool, *args],
        cwd=cwd, text=True, capture_output=True, check=False, env=env,
    )


def run_source(text, argv=None):
    module = parse_source(Source.from_text("<test>", text))
    return Interpreter(module, argv or []).run_main()


class LexerTests(unittest.TestCase):
    def test_indent_dedent_balanced(self):
        toks = tokenize(Source.from_text("t", "fn main() -> int\n    return 0\n"))
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds.count("INDENT"), kinds.count("DEDENT"))
        self.assertEqual(kinds.count("INDENT"), 1)

    def test_tab_indentation_rejected(self):
        from axonc.errors import Diagnostic
        with self.assertRaises(Diagnostic):
            tokenize(Source.from_text("t", "fn main()\n\treturn 0\n"))

    def test_numeric_suffix_is_one_token(self):
        toks = [t for t in tokenize(Source.from_text("t", "x = 0_u64\n")) if t.kind == "INT"]
        self.assertEqual(toks[0].value, "0_u64")


class ParserTests(unittest.TestCase):
    def test_all_core_examples_parse(self):
        for name in CORE_EXAMPLES:
            with self.subTest(name=name):
                parse_path(EXAMPLES / f"{name}.ax")

    def test_struct_and_impl(self):
        module = parse_path(EXAMPLES / "09_struct_trait.ax")
        names = [type(i).__name__ for i in module.items]
        self.assertIn("StructDef", names)
        self.assertIn("ImplBlock", names)
        self.assertIn("TraitDef", names)


class CheckerTests(unittest.TestCase):
    def test_core_examples_have_no_diagnostics(self):
        for name in CORE_EXAMPLES:
            with self.subTest(name=name):
                diags = check_module(parse_path(EXAMPLES / f"{name}.ax"))
                self.assertEqual(diags, [], [d.message for d in diags])

    def test_unknown_symbol_reported(self):
        diags = check_module(parse_source(Source.from_text(
            "t", "fn main() -> int\n    return missing_value\n")))
        self.assertTrue(any(d.code == "E100" for d in diags))

    def test_duplicate_function_reported(self):
        diags = check_module(parse_source(Source.from_text(
            "t", "fn f() -> int\n    return 1\nfn f() -> int\n    return 2\n")))
        self.assertTrue(any(d.code == "E101" for d in diags))

    def test_unknown_field_reported(self):
        src = "struct P\n    x: int\nfn main() -> int\n    let p = P{ y: 1 }\n    return 0\n"
        diags = check_module(parse_source(Source.from_text("t", src)))
        self.assertTrue(any(d.code == "E103" for d in diags))


class InterpreterTests(unittest.TestCase):
    def test_arithmetic_and_recursion(self):
        src = ("fn fib(n: int) -> int\n"
               "    if n < 2 then\n        return n\n"
               "    return fib(n - 1) + fib(n - 2)\n"
               "fn main() -> int\n    return fib(10)\n")
        self.assertEqual(run_source(src), 55)

    def test_hello_output(self):
        result = axon("axc", "run", "--interpret", str(EXAMPLES / "01_hello.ax"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Hello, World")

    def test_fibonacci_output(self):
        result = axon("axc", "run", "--interpret", str(EXAMPLES / "03_fibonacci.ax"))
        self.assertIn("recursive: 55", result.stdout)
        self.assertIn("iterative: 55", result.stdout)

    def test_struct_trait_output(self):
        result = axon("axc", "run", "--interpret", str(EXAMPLES / "09_struct_trait.ax"))
        self.assertIn("Circle(radius: 4.0)", result.stdout)
        self.assertIn("area: 50.26548245743669", result.stdout)

    def test_match_and_option(self):
        src = ("fn classify(n: int) -> str\n"
               "    match n do\n"
               "        case 0 then return \"zero\"\n"
               "        case _ then return \"other\"\n"
               "fn main() -> int\n"
               "    println(classify(0))\n    println(classify(7))\n    return 0\n")
        module = parse_source(Source.from_text("t", src))
        self.assertEqual(Interpreter(module).run_main(), 0)


class CheckCommandTests(unittest.TestCase):
    def test_check_passes_on_core_examples(self):
        files = [str(EXAMPLES / f"{n}.ax") for n in CORE_EXAMPLES]
        result = axon("axc", "check", *files)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_fails_on_bad_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "bad.ax"
            bad.write_text("fn main() -> int\n    return undefined_symbol\n")
            result = axon("axc", "check", str(bad))
            self.assertEqual(result.returncode, 1)
            self.assertIn("E100", result.stderr)


@unittest.skipUnless(HAVE_CC, "a C compiler is required for native backend tests")
class NativeBackendTests(unittest.TestCase):
    def test_native_examples_generate_valid_c(self):
        for name in NATIVE_EXAMPLES:
            with self.subTest(name=name):
                c = generate_c(parse_path(EXAMPLES / f"{name}.ax"))
                self.assertIn("int main(", c)

    def test_compile_produces_native_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = native_path(pathlib.Path(tmp) / "hello")
            result = axon("axc", "compile", str(EXAMPLES / "01_hello.ax"), "-o", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("native", result.stdout)
            self.assertTrue(out.exists())
            # genuine native binary: ELF, Mach-O, or PE — not a script
            self.assertTrue(is_native_binary(out), out.read_bytes()[:4])
            run = subprocess.run([str(out)], cwd=tmp, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "Hello, World")

    def test_native_fibonacci_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = native_path(pathlib.Path(tmp) / "fib")
            axon("axc", "compile", str(EXAMPLES / "03_fibonacci.ax"), "-o", str(out))
            run = subprocess.run([str(out)], cwd=tmp, text=True, capture_output=True)
            self.assertIn("recursive: 55", run.stdout)

    def test_portable_fallback_for_non_native_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            (tmpdir / "main.ax").write_text((EXAMPLES / "05_json_file.ax").read_text())
            (tmpdir / "config.json").write_text('{"name":"axon"}')
            out = tmpdir / "app"
            result = axon("axc", "compile", "main.ax", "-o", "app", cwd=tmpdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("portable", result.stderr)
            # portable artifacts are python launchers; invoke via the
            # interpreter on Windows where shebangs don't apply
            cmd = [str(out)] if os.name != "nt" else [sys.executable, str(out)]
            run = subprocess.run(cmd, cwd=tmpdir, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "name: axon")


class StdlibExampleTests(unittest.TestCase):
    def test_json_file_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            (tmpdir / "main.ax").write_text((EXAMPLES / "05_json_file.ax").read_text())
            (tmpdir / "config.json").write_text('{"name":"axon"}')
            result = axon("axc", "run", "--interpret", "main.ax", cwd=tmpdir)
            self.assertEqual(result.stdout.strip(), "name: axon", result.stderr)

    def test_cli_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            (tmpdir / "main.ax").write_text((EXAMPLES / "07_cli_tool.ax").read_text())
            (tmpdir / "input.txt").write_text("a\nb\nc\n")
            result = axon("axc", "run", "--interpret", "main.ax", "input.txt", cwd=tmpdir)
            self.assertEqual(result.stdout.strip(), "input.txt: 4", result.stderr)

    def test_error_chain_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            (tmpdir / "main.ax").write_text((EXAMPLES / "10_error_chain.ax").read_text())
            (tmpdir / "server.json").write_text('{"name":"svc","port":8080}')
            result = axon("axc", "run", "--interpret", "main.ax", cwd=tmpdir)
            self.assertEqual(result.stdout.strip(), "starting svc on 8080", result.stderr)


class FormatterAndLinterTests(unittest.TestCase):
    def test_formatter_idempotent_on_examples(self):
        result = axon("axfmt", "--check", str(EXAMPLES))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_linter_clean_on_examples(self):
        for name in CORE_EXAMPLES:
            with self.subTest(name=name):
                result = axon("axlint", str(EXAMPLES / f"{name}.ax"))
                self.assertEqual(result.returncode, 0, result.stderr)


class ProjectWorkflowTests(unittest.TestCase):
    def test_new_check_build_run_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp) / "demo"
            self.assertEqual(axon("axbuild", "new", str(project)).returncode, 0)
            self.assertEqual(axon("axbuild", "check", cwd=project).returncode, 0)
            build = axon("axbuild", "build", cwd=project)
            self.assertEqual(build.returncode, 0, build.stderr)
            binary = project / "target" / "debug" / "hello"
            if HAVE_CC:
                binary = native_path(binary)
                self.assertTrue(binary.exists())
                self.assertTrue(is_native_binary(binary))
            else:
                self.assertTrue(binary.exists())
            run = axon("axbuild", "run", cwd=project)
            self.assertEqual(run.stdout.strip(), "Hello, World", run.stderr)
            self.assertEqual(axon("axbuild", "test", cwd=project).returncode, 0)


if __name__ == "__main__":
    unittest.main()
