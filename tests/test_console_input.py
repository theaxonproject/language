"""Console input behaves the same in native programs and the interpreter."""

import pathlib
import subprocess
import sys
import tempfile
import unittest

from test_toolchain import axon, AXON_TOOL, HAVE_CC, native_path, is_native_binary


@unittest.skipUnless(HAVE_CC, "a C compiler is required")
class ConsoleInputTests(unittest.TestCase):
    def test_lines_eof_and_long_unicode_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "input.ax"
            source.write_text(
                'fn main() -> int\n'
                '    loop do\n'
                '        let line = read_line()\n'
                '        if line.is_none() then\n'
                '            return 0\n'
                '        println(f"[{line.unwrap()}]")\n'
                '    return 0\n')
            binary = native_path(pathlib.Path(tmp) / "input")
            compiled = axon("axc", "compile", str(source), "-o", str(binary))
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertTrue(is_native_binary(binary))
            long_line = "Zażółć gęślą jaźń 🐱 " * 1000
            for stdin, expected in (("", ""), ("\nhello\r\nlast", "[]\n[hello]\n[last]\n"),
                                    (long_line + "\n", "[" + long_line + "]\n")):
                for command in ([str(binary)],
                                [sys.executable, str(AXON_TOOL), "axc", "run", "--interpret", str(source)]):
                    with self.subTest(mode=command[0], input_length=len(stdin)):
                        result = subprocess.run(command, input=stdin, text=True,
                                                capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
