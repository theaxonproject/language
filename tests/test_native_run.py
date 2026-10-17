"""The run command must execute compiled code and never silently interpret."""

import pathlib
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from test_toolchain import axon, HAVE_CC, EXAMPLES, is_native_binary
from axonc import driver


class RunCommandTests(unittest.TestCase):
    def test_help_and_missing_source(self):
        result = axon("axc", "run", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--interpret", result.stdout)
        self.assertEqual(axon("axc", "run").returncode, 2)

    def test_missing_compiler_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CC=str(pathlib.Path(tmp) / "missing-cc"))
            result = axon("axc", "run", str(EXAMPLES / "01_hello.ax"), env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("E810", result.stderr)
        self.assertIn("C compiler", result.stderr)
        self.assertNotIn("E999", result.stderr)

    def test_unsupported_feature_requires_explicit_interpretation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "main.ax"
            path.write_text('fn main() -> int\n    let double = x => x * 2\n'
                            '    println(double(21))\n    return 0\n')
            native = axon("axc", "run", str(path))
            interpreted = axon("axc", "run", "--interpret", str(path))
        self.assertEqual(native.returncode, 1)
        self.assertEqual(native.stdout, "")
        self.assertIn("E800", native.stderr)
        self.assertIn("--interpret", native.stderr)
        self.assertEqual(interpreted.returncode, 0, interpreted.stderr)
        self.assertEqual(interpreted.stdout, "42\n")

    def test_invalid_program_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "main.ax"
            path.write_text('fn main() -> int\n    println("must not run")\n'
                            '    return undefined_symbol\n')
            result = axon("axc", "run", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("E100", result.stderr)


@unittest.skipUnless(HAVE_CC, "a C compiler is required for native run tests")
class NativeRunTests(unittest.TestCase):
    def test_real_binary_arguments_stdio_and_cleanup(self):
        run_process = subprocess.run
        executables = []
        program_args = ["argument with spaces", "--release", "; literal"]

        def inspect_then_run(command, **kwargs):
            # Compiler invocations capture output; program execution inherits
            # stdin/stdout/stderr and cwd from the caller.
            if not kwargs:
                executable = pathlib.Path(command[0])
                self.assertTrue(is_native_binary(executable))
                self.assertEqual(command[1:], program_args)
                executables.append(executable)
            return run_process(command, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "main.ax"
            source.write_text('fn main() -> int\n    return 23\n')
            with mock.patch.object(driver.subprocess, "run", side_effect=inspect_then_run), \
                 mock.patch.object(driver, "Interpreter", side_effect=AssertionError("must compile")):
                self.assertEqual(driver.run_native_path(source, program_args), 23)
        self.assertEqual(len(executables), 1)
        self.assertFalse(executables[0].parent.exists())

    def test_native_output_cwd_and_exit_status_in_both_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "input.txt").write_text("native file IO\n")
            source = root / "program with spaces.ax"
            source.write_text('use Core\nuse Io.{ File }\nfn main() -> int\n'
                              '    print(File.open("input.txt").read_all_text())\n'
                              '    eprintln("stderr works")\n    return 17\n')
            for flags in ([], ["--release"]):
                with self.subTest(flags=flags):
                    result = axon("axc", "run", *flags, str(source), cwd=root)
                    self.assertEqual(result.returncode, 17, result.stderr)
                    self.assertEqual(result.stdout, "native file IO\n")
                    self.assertEqual(result.stderr, "stderr works\n")

    def test_panic_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "main.ax"
            source.write_text('fn main() -> int\n    let v = [1]\n'
                              '    println(v[5])\n    return 0\n')
            result = axon("axc", "run", str(source))
        self.assertEqual(result.returncode, 101)
        self.assertIn("index out of range", result.stderr)


if __name__ == "__main__":
    unittest.main()
