"""Native-backend feature tests.

Each test compiles an AXON program to a native executable AND runs the same
program through the interpreter, asserting that both succeed and produce
identical output. This pins the central toolchain contract: identical
observable behavior between `axc run --interpret` and a compiled binary.
"""

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

sys.path.insert(0, str(TOOLS))

HAVE_CC = any(shutil.which(cc) for cc in ("cc", "gcc", "clang", "cl"))


def produced_binary(out: pathlib.Path) -> pathlib.Path:
    exe = out.with_suffix(out.suffix + ".exe")
    return exe if (os.name == "nt" and exe.exists()) else out


def compile_and_run(source: str, expect_native: bool = True,
                    setup=None) -> tuple[str, str]:
    """Return (interpreter stdout, native stdout) for `source`."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        src = tmpdir / "main.ax"
        src.write_text(source, encoding="utf-8")
        if setup:
            setup(tmpdir)
        interp = subprocess.run(
            [sys.executable, str(AXON_TOOL), "axc", "run", "--interpret", "main.ax"],
            cwd=tmpdir, text=True, capture_output=True)
        assert interp.returncode == 0, interp.stderr
        out = tmpdir / "app"
        compiled = subprocess.run(
            [sys.executable, str(AXON_TOOL), "axc", "compile", "main.ax",
             "-o", str(out)],
            cwd=tmpdir, text=True, capture_output=True)
        assert compiled.returncode == 0, compiled.stderr
        if expect_native:
            assert "native" in compiled.stdout, compiled.stdout + compiled.stderr
        native = subprocess.run([str(produced_binary(out))], cwd=tmpdir,
                                text=True, capture_output=True)
        assert native.returncode == 0, native.stderr
        return interp.stdout, native.stdout


@unittest.skipUnless(HAVE_CC, "a C compiler is required for native backend tests")
class NativeParityTests(unittest.TestCase):
    def check(self, source: str, setup=None) -> str:
        interp_out, native_out = compile_and_run(source, setup=setup)
        self.assertEqual(interp_out, native_out)
        return native_out

    def test_global_constants(self):
        out = self.check(
            "use Core\n"
            "const GREETING: str = \"hello\"\n"
            "const LIMIT = 3\n"
            "fn main() -> int\n"
            "    for i in 0..LIMIT do\n"
            "        println(f\"{GREETING} {i}\")\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), ["hello 0", "hello 1", "hello 2"])

    def test_boolean_operators(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    let a = 5\n"
            "    if a > 1 and a < 10 then\n"
            "        println(\"in range\")\n"
            "    if a < 0 or a == 5 then\n"
            "        println(\"matched\")\n"
            "    if not (a == 6) then\n"
            "        println(\"not six\")\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), ["in range", "matched", "not six"])

    def test_match_literals_guards_or_patterns(self):
        out = self.check(
            "use Core\n"
            "fn classify(n: int) -> str\n"
            "    let label = match n do\n"
            "        case 0 then \"zero\"\n"
            "        case 1 | 2 then \"small\"\n"
            "        case x if x < 0 then \"negative\"\n"
            "        case _ then \"big\"\n"
            "    return label\n"
            "fn main() -> int\n"
            "    println(classify(0), classify(2), classify(-4), classify(50))\n"
            "    return 0\n")
        self.assertEqual(out.strip(), "zero small negative big")

    def test_match_on_strings(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    let cmd = \"stop\"\n"
            "    match cmd do\n"
            "        case \"go\" then println(\"moving\")\n"
            "        case \"stop\" then println(\"halted\")\n"
            "        case _ then println(\"unknown\")\n"
            "    return 0\n")
        self.assertEqual(out.strip(), "halted")

    def test_enums_without_payload(self):
        out = self.check(
            "use Core\n"
            "enum Color\n    Red\n    Green\n    Blue\n"
            "fn name(c: Color) -> str\n"
            "    match c do\n"
            "        case Red then return \"red\"\n"
            "        case Green then return \"green\"\n"
            "        case Blue then return \"blue\"\n"
            "fn main() -> int\n"
            "    println(name(Green), name(Blue))\n"
            "    let c = Red\n"
            "    println(c == Red, c == Blue)\n"
            "    println(f\"{c}\")\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), ["green blue", "true false", "Red"])

    def test_enums_with_payload(self):
        out = self.check(
            "use Core\n"
            "enum Shape\n"
            "    Circle(radius: float64)\n"
            "    Rect(w: float64, h: float64)\n"
            "    Dot\n"
            "fn area(s: Shape) -> float64\n"
            "    match s do\n"
            "        case Circle(r) then return 3.0 * r * r\n"
            "        case Rect(w, h) then return w * h\n"
            "        case Dot then return 0.0\n"
            "fn main() -> int\n"
            "    println(area(Circle(radius: 2.0)), area(Rect(w: 3.0, h: 4.0)), area(Dot))\n"
            "    println(f\"{Circle(radius: 1.5)}\")\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), ["12.0 12.0 0.0", "Circle(1.5)"])

    def test_option_some_none_and_if_let(self):
        out = self.check(
            "use Core\n"
            "fn find(limit: int) -> int?\n"
            "    for i in 0..limit do\n"
            "        if i == 3 then\n"
            "            return Some(i)\n"
            "    return none\n"
            "fn main() -> int\n"
            "    if let Some(x) = find(10) then\n"
            "        println(f\"found {x}\")\n"
            "    match find(2) do\n"
            "        case Some(x) then println(\"unexpected\")\n"
            "        case none then println(\"nothing\")\n"
            "    let a = find(10)\n"
            "    println(a.is_some(), a.unwrap(), find(1).unwrap_or(-1))\n"
            "    println(f\"{find(10)} {find(0)}\")\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(),
                         ["found 3", "nothing", "true 3 -1", "Some(3) none"])

    def test_result_try_propagation_and_catch(self):
        out = self.check(
            "use Core\n"
            "fn parse_port(text: str) -> int!str\n"
            "    let n = try text.parse(int)\n"
            "    if n < 1 or n > 65535 then\n"
            "        return Err(\"port out of range\")\n"
            "    return Ok(n)\n"
            "fn main() -> int\n"
            "    let good = try parse_port(\"8080\") or 0\n"
            "    let bad = try parse_port(\"oops\") or -1\n"
            "    println(good, bad)\n"
            "    match parse_port(\"99999\") do\n"
            "        case Ok(p) then println(f\"ok {p}\")\n"
            "        case Err(e) then println(f\"err {e}\")\n"
            "    let fallback = try parse_port(\"nope\") catch e then 42\n"
            "    println(fallback)\n"
            "    let r = parse_port(\"80\")\n"
            "    println(r.is_ok(), r.unwrap())\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(),
                         ["8080 -1", "err port out of range", "42", "true 80"])

    def test_let_try_catch_block(self):
        out = self.check(
            "use Core\n"
            "fn halve(n: int) -> int!str\n"
            "    if n % 2 != 0 then\n"
            "        return Err(\"odd\")\n"
            "    return Ok(n / 2)\n"
            "fn main() -> int\n"
            "    let x = try halve(10) catch e then\n"
            "        eprintln(f\"error: {e}\")\n"
            "        return 1\n"
            "    println(x)\n"
            "    return 0\n")
        self.assertEqual(out.strip(), "5")

    def test_vectors(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    let mut v = [3, 1, 2]\n"
            "    v.push(10)\n"
            "    v[0] = 4\n"
            "    println(f\"{v} {v.len()} {v.sum()} {v.sort()} {v.reverse()}\")\n"
            "    println(f\"{v.first()} {v.last()} {v.get(99)} {v.contains(10)}\")\n"
            "    let total = 0\n"
            "    let mut acc = 0\n"
            "    for x in v do\n"
            "        acc += x\n"
            "    println(acc, v[1..3])\n"
            "    let words = [\"b\", \"a\"]\n"
            "    println(words.sort().join(\",\"))\n"
            "    let zeros = [0; 3]\n"
            "    println(f\"{zeros}\")\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), [
            "[4, 1, 2, 10] 4 17 [1, 2, 4, 10] [10, 2, 1, 4]",
            "Some(4) Some(10) none true",
            "17 [1, 2]",
            "a,b",
            "[0, 0, 0]",
        ])

    def test_maps(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    let mut ages: Map(str, int) = Map.new()\n"
            "    ages.insert(\"ada\", 36)\n"
            "    ages[\"grace\"] = 85\n"
            "    ages[\"ada\"] = 37\n"
            "    let a = ages.get(\"ada\")\n"
            "    let missing = ages.get(\"bob\")\n"
            "    let has = ages.contains_key(\"grace\")\n"
            "    println(f\"{ages} {a} {missing} {has} {ages.len()}\")\n"
            "    let removed = ages.remove(\"ada\")\n"
            "    println(f\"{removed} {ages.len()} {ages.keys()}\")\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), [
            "{ada: 37, grace: 85} Some(37) none true 2",
            "Some(37) 1 [grace]",
        ])

    def test_string_methods(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    let s = \"  Hello, AXON!  \"\n"
            "    println(s.trim().to_upper(), s.trim().to_lower())\n"
            "    println(s.trim().len(), s.trim().is_empty(), \"\".is_empty())\n"
            "    let has = s.contains(\"AXON\")\n"
            "    let starts = s.trim().starts_with(\"Hello\")\n"
            "    println(has, starts, s.trim().ends_with(\"!\"))\n"
            "    println(\"a,b,,c\".split(\",\").join(\"|\"))\n"
            "    println(\"one two  three\".split().join(\"+\"))\n"
            "    println(\"x\".repeat(3), \"abcdef\".slice(1..4), \"abcdef\"[2])\n"
            "    println(\"a-b\".replace(\"-\", \"_\"))\n"
            "    let lines = \"l1\\nl2\\n\".lines()\n"
            "    println(f\"{lines}\")\n"
            "    println(\"abc\".chars().len(), \"abc\".compare(\"abd\"))\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), [
            "HELLO, AXON! hello, axon!",
            "12 false true",
            "true true true",
            "a|b||c",
            "one+two+three",
            "xxx bcd c",
            "a_b",
            "[l1, l2]",
            "3 -1",
        ])

    def test_numeric_methods_and_casts(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    let x = -7\n"
            "    println(x.abs(), x.clamp(0, 10), 2.pow(10), x.max(3), x.min(3))\n"
            "    println(3.7.floor(), 3.2.ceil(), 2.5.sqrt(), 3.6.round())\n"
            "    println((3.9 as int), (5 as float64), (65 as str))\n"
            "    println(min(2, 9), max(2, 9), abs(-4))\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), [
            "7 0 1024 3 -7",
            "3 4 1.5811388300841898 4",
            "3 5.0 65",
            "2 9 4",
        ])

    def test_file_io(self):
        out = self.check(
            "use Core\nuse Io.{ File }\n"
            "fn main() -> int\n"
            "    let ok = try File.write(\"data.txt\", \"alpha\\nbeta\\n\") or false\n"
            "    println(ok)\n"
            "    print(File.open(\"data.txt\").read_all_text())\n"
            "    for line in File.open(\"data.txt\").read_lines() do\n"
            "        println(f\"> {line}\")\n"
            "    println(File.exists(\"data.txt\"), File.exists(\"nope.txt\"))\n"
            "    let text = try File.read_to_string(\"missing.txt\") or \"gone\"\n"
            "    println(text)\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), [
            "true", "alpha", "beta", "> alpha", "> beta", "true false", "gone",
        ])

    def test_structs_with_vec_fields_and_defaults(self):
        out = self.check(
            "use Core\n"
            "struct Basket\n"
            "    label: str\n"
            "    items: Vec(str)\n"
            "    count: int = 0\n"
            "fn main() -> int\n"
            "    let mut b = Basket{ label: \"fruit\", items: [\"apple\"] }\n"
            "    b.items.push(\"pear\")\n"
            "    b.count += 2\n"
            "    println(f\"{b}\")\n"
            "    return 0\n")
        self.assertEqual(out.strip(),
                         "Basket{ label: fruit, items: [apple, pear], count: 2 }")

    def test_assertions(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    assert(1 + 1 == 2, \"math\")\n"
            "    assert_eq(2 * 3, 6)\n"
            "    assert_eq(\"a\" + \"b\", \"ab\")\n"
            "    println(\"all good\")\n"
            "    return 0\n")
        self.assertEqual(out.strip(), "all good")

    def test_while_and_compound_assignment(self):
        out = self.check(
            "use Core\n"
            "fn main() -> int\n"
            "    let mut n = 1\n"
            "    while n < 100 do\n"
            "        n *= 3\n"
            "    println(n)\n"
            "    let mut s = \"x\"\n"
            "    s += \"y\"\n"
            "    println(s)\n"
            "    return 0\n")
        self.assertEqual(out.splitlines(), ["243", "xy"])


@unittest.skipUnless(HAVE_CC, "a C compiler is required for native backend tests")
class NativeRuntimeBehaviorTests(unittest.TestCase):
    def test_native_panic_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            (tmpdir / "main.ax").write_text(
                "use Core\n"
                "fn main() -> int\n"
                "    let v = [1]\n"
                "    println(v[5])\n"
                "    return 0\n")
            out = tmpdir / "app"
            compiled = subprocess.run(
                [sys.executable, str(AXON_TOOL), "axc", "compile", "main.ax",
                 "-o", str(out)], cwd=tmpdir, text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            run = subprocess.run([str(produced_binary(out))], cwd=tmpdir,
                                 text=True, capture_output=True)
            self.assertEqual(run.returncode, 101)
            self.assertIn("index out of range", run.stderr)

    def test_unsupported_feature_falls_back_to_portable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            (tmpdir / "main.ax").write_text(
                "use Core\n"
                "fn main() -> int\n"
                "    let double = x => x * 2\n"
                "    println(double(21))\n"
                "    return 0\n")
            out = tmpdir / "app"
            compiled = subprocess.run(
                [sys.executable, str(AXON_TOOL), "axc", "compile", "main.ax",
                 "-o", str(out)], cwd=tmpdir, text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertIn("portable", compiled.stderr)
            cmd = [str(out)] if os.name != "nt" else [sys.executable, str(out)]
            run = subprocess.run(cmd, cwd=tmpdir, text=True,
                                 capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "42")


if __name__ == "__main__":
    unittest.main()
