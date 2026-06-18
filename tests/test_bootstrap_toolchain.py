import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "axon_tool.py"
AXC = ROOT / "dist" / "x86_64-apple-darwin" / "bin" / "axc"
AXFMT = ROOT / "dist" / "x86_64-apple-darwin" / "bin" / "axfmt"
AXBUILD = ROOT / "dist" / "x86_64-apple-darwin" / "bin" / "axbuild"


spec = importlib.util.spec_from_file_location("axon_tool", TOOL)
axon_tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["axon_tool"] = axon_tool
spec.loader.exec_module(axon_tool)


def run(*args, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


class BootstrapToolchainTests(unittest.TestCase):
    def test_lexer_validates_indentation_and_numeric_suffixes(self):
        tokens = axon_tool.lex_expr("0_u64 + 0xFF_u16 + 1..10")
        values = [token.value for token in tokens if token.kind != "EOF"]
        self.assertEqual(values, ["0_u64", "+", "0xFF_u16", "+", "1", "..", "10"])

        with self.assertRaises(axon_tool.LexError):
            axon_tool.logical_lines("fn main()\n\treturn 0\n", ROOT / "bad.ax")

    def test_all_examples_parse(self):
        result = run(str(AXC), "check", *map(str, sorted((ROOT / "examples").glob("*.ax"))))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_formatter_is_idempotent_on_examples(self):
        result = run(str(AXFMT), "--check", str(ROOT / "examples"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_examples_run(self):
        hello = run(str(AXC), "run", str(ROOT / "examples" / "01_hello.ax"))
        self.assertEqual(hello.returncode, 0, hello.stderr)
        self.assertEqual(hello.stdout.strip(), "Hello, World")

        fib = run(str(AXC), "run", str(ROOT / "examples" / "03_fibonacci.ax"))
        self.assertEqual(fib.returncode, 0, fib.stderr)
        self.assertIn("recursive: 55", fib.stdout)
        self.assertIn("iterative: 55", fib.stdout)

        shape = run(str(AXC), "run", str(ROOT / "examples" / "09_struct_trait.ax"))
        self.assertEqual(shape.returncode, 0, shape.stderr)
        self.assertIn("Circle(radius: 4.0)", shape.stdout)
        self.assertIn("area: 50.26548245743669", shape.stdout)

    def test_compile_produces_standalone_executable(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            output = tmp / "hello"
            compiled = run(str(AXC), "compile", str(ROOT / "examples" / "01_hello.ax"), "-o", str(output))
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertTrue(output.exists())
            text = output.read_text(encoding="utf-8")
            self.assertIn("RUNTIME_PAYLOAD", text)
            self.assertNotIn("exec python3", text)

            result = run(str(output), cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "Hello, World")

    def test_stdlib_backed_examples_run_with_local_fixtures(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            (tmp / "main.ax").write_text((ROOT / "examples" / "05_json_file.ax").read_text(), encoding="utf-8")
            (tmp / "config.json").write_text('{"name":"axon"}', encoding="utf-8")
            result = run(str(AXC), "run", "main.ax", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "name: axon")

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            (tmp / "main.ax").write_text((ROOT / "examples" / "07_cli_tool.ax").read_text(), encoding="utf-8")
            (tmp / "input.txt").write_text("a\nb\nc\n", encoding="utf-8")
            result = run(str(AXC), "run", "main.ax", "input.txt", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "input.txt: 4")

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            (tmp / "main.ax").write_text((ROOT / "examples" / "10_error_chain.ax").read_text(), encoding="utf-8")
            (tmp / "server.json").write_text('{"name":"svc","port":8080}', encoding="utf-8")
            result = run(str(AXC), "run", "main.ax", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "starting svc on 8080")

    def test_axbuild_new_check_and_run(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            project = tmp / "demo"
            result = run(str(AXBUILD), "new", str(project))
            self.assertEqual(result.returncode, 0, result.stderr)

            check = run(str(AXBUILD), "check", cwd=project)
            self.assertEqual(check.returncode, 0, check.stderr)

            build = run(str(AXBUILD), "build", cwd=project)
            self.assertEqual(build.returncode, 0, build.stderr)
            built_binary = project / "target" / "debug" / "hello"
            self.assertTrue(built_binary.exists())
            self.assertIn("RUNTIME_PAYLOAD", built_binary.read_text(encoding="utf-8"))

            app = run(str(AXBUILD), "run", cwd=project)
            self.assertEqual(app.returncode, 0, app.stderr)
            self.assertEqual(app.stdout.strip(), "Hello, World")


if __name__ == "__main__":
    unittest.main()
