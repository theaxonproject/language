"""Tests for the 1.2 language work.

Covers the static type checker (every diagnostic it introduces, plus the
absence of false positives on real programs), the interpreter semantics that
were tightened (checked arithmetic, block-scoped `defer`, closures, deep
recursion), native/interpreter parity for the newly native features
(closures, tuples, generics, `defer`, combinators, evaluation order), the
`@test` runner, the REPL, and the new lints.
"""

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

from axonc.checker import check_module  # noqa: E402
from axonc.errors import Source  # noqa: E402
from axonc.parser import parse_source  # noqa: E402

HAVE_CC = any(shutil.which(cc) for cc in ("cc", "gcc", "clang", "cl"))


def codes(source: str) -> list[str]:
    module = parse_source(Source.from_text("t.ax", source))
    return [d.code for d in check_module(module)]


def messages(source: str) -> list[str]:
    module = parse_source(Source.from_text("t.ax", source))
    return [d.message for d in check_module(module)]


def program(*body: str, header: str = "") -> str:
    lines = "".join(f"    {line}\n" for line in body)
    return f"use Core\n{header}fn main() -> int\n{lines}    return 0\n"


def axon(args, cwd, stdin=None):
    return subprocess.run([sys.executable, str(AXON_TOOL), *args], cwd=cwd,
                          text=True, capture_output=True, input=stdin)


def run_both(source: str, args=(), release=False):
    """Run `source` with the interpreter and as a native binary.

    Returns ((rc, stdout, stderr) interpreted, (rc, stdout, stderr) native)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "main.ax"
        src.write_text(source, encoding="utf-8")
        interp = axon(["axc", "run", "--interpret", "main.ax", *args], tmp)
        flags = ["--release"] if release else []
        native = axon(["axc", "run", *flags, "main.ax", *args], tmp)
        return ((interp.returncode, interp.stdout, interp.stderr),
                (native.returncode, native.stdout, native.stderr))


class TypeCheckerTests(unittest.TestCase):
    def test_examples_type_check_cleanly(self):
        for path in sorted(EXAMPLES.glob("*.ax")):
            if path.name.startswith("11_"):
                continue  # the aspirational full-stdlib showcase
            with self.subTest(example=path.name):
                module = parse_source(Source.from_path(path))
                self.assertEqual([d.render() for d in check_module(module)], [])

    def test_let_annotation_mismatch(self):
        self.assertEqual(codes(program('let x: int = "hello"')), ["E110"])

    def test_integer_widens_to_float_but_not_back(self):
        self.assertEqual(codes(program("let x: float64 = 3")), [])
        self.assertEqual(codes(program("let x: int = 3.5")), ["E110"])

    def test_argument_type_and_count(self):
        header = "fn add(a: int, b: int) -> int\n    return a + b\n"
        self.assertEqual(codes(program('let y = add(1, "two")', header=header)), ["E110"])
        self.assertEqual(codes(program("let y = add(1)", header=header)), ["E111"])
        self.assertEqual(codes(program("let y = add(1, 2, 3)", header=header)), ["E111"])
        self.assertEqual(codes(program("let y = add(a: 1, c: 2)", header=header)),
                         ["E111", "E111"])
        self.assertEqual(codes(program("let y = add(b: 1, a: 2)", header=header)), [])

    def test_operator_mismatch_has_a_hint(self):
        src = program('let s = "total: " + 3')
        self.assertEqual(codes(src), ["E110"])
        module = parse_source(Source.from_text("t.ax", src))
        self.assertIn("f-string", check_module(module)[0].help_text)

    def test_unknown_methods_and_fields(self):
        self.assertEqual(codes(program("let v = [1, 2]", "println(v.lenght())")), ["E104"])
        self.assertEqual(codes(program("let v = [1, 2]", "println(v.len)")), ["E103"])
        header = "struct P\n    x: int\n"
        self.assertEqual(codes(program("let p = P{ x: 1 }", "println(p.y)", header=header)),
                         ["E103"])
        self.assertEqual(codes(program("let p = P{ x: 1 }", "p.go()", header=header)), ["E104"])

    def test_return_checks(self):
        self.assertEqual(codes("fn f() -> int\n    return \"no\"\nfn main()\n    return\n"),
                         ["E110"])
        self.assertEqual(codes("fn f()\n    return 3\nfn main()\n    return\n"), ["E121"])
        self.assertEqual(codes("fn f() -> int\n    return\nfn main()\n    return\n"), ["E121"])

    def test_missing_return(self):
        src = ("fn sign(n: int) -> str\n"
               "    if n > 0 then\n        return \"+\"\n"
               "    else if n < 0 then\n        return \"-\"\n"
               "fn main()\n    return\n")
        self.assertEqual(codes(src), ["E123"])
        ok = src.replace("        return \"-\"\n", "        return \"-\"\n    else\n        return \"0\"\n")
        self.assertEqual(codes(ok), [])
        self.assertEqual(codes("fn spin() -> int\n    loop do\n        println(1)\n"
                               "fn main()\n    return\n"), [])

    def test_exhaustiveness(self):
        header = "enum Color\n    Red\n    Green\n    Blue\n"
        partial = program("let c = Red", "match c do",
                          "    case Red then println(1)",
                          "    case Green then println(2)", header=header)
        self.assertEqual(codes(partial), ["E112"])
        self.assertIn("`Blue`", messages(partial)[0])
        full = partial.replace("    case Green then println(2)\n",
                               "    case Green | Blue then println(2)\n")
        self.assertEqual(codes(full), [])
        nested = program(
            "let a: int? = Some(1)", "let b: int!str = Ok(2)",
            "let s = match (a, b) do",
            "    case (Some(x), Ok(y)) then x + y",
            "    case (Some(x), Err(_)) then x",
            "    case (None, _) then 0",
            "println(s)")
        self.assertEqual(codes(nested), [])
        self.assertEqual(codes(nested.replace("        case (None, _) then 0\n", "")), ["E112"])
        ints = program("let n = 3", "match n do", "    case 1 then println(1)")
        self.assertEqual(codes(ints), ["E112"])
        guarded = program("let n: int? = Some(3)", "match n do",
                          "    case Some(x) if x > 1 then println(x)",
                          "    case None then println(0)")
        self.assertEqual(codes(guarded), ["E112"])

    def test_immutability(self):
        self.assertEqual(codes(program("let n = 1", "n = 2")), ["E122"])
        self.assertEqual(codes(program("let mut n = 1", "n = 2")), [])
        self.assertEqual(codes(program("let v = [1]", "v.push(2)")), ["E122"])
        self.assertEqual(codes(program("let v = [1]", "v[0] = 2")), ["E122"])
        header = ("struct C\n    n: int\n"
                  "impl C\n    fn bump(self)\n        self.n += 1\n")
        self.assertEqual(codes(program(header=header)), ["E122"])
        self.assertEqual(codes(program(header=header.replace("bump(self)", "bump(mut self)"))), [])

    def test_catch_block_must_exit(self):
        src = program("let n = try \"x\".parse(int) catch e then", '    println(e)', "println(n)")
        self.assertEqual(codes(src), ["E124"])
        self.assertEqual(codes(src.replace("    println(e)\n", "    println(e)\n        return 1\n")),
                         [])

    def test_try_requires_a_fallible_function(self):
        src = "fn f() -> int\n    let n = try \"4\".parse(int)\n    return n\nfn main()\n    return\n"
        self.assertEqual(codes(src), ["E110"])
        self.assertEqual(codes(src.replace("-> int", "-> int!str").replace("return n", "return Ok(n)")), [])

    def test_loop_control_outside_loop(self):
        self.assertEqual(codes(program("break")), ["E125"])

    def test_missing_trait_method(self):
        header = ("trait Shape\n    fn area(self) -> float64\n"
                  "    fn name(self) -> str = \"shape\"\n"
                  "struct Sq\n    side: float64\n"
                  "impl Sq as Shape\n    fn perimeter(self) -> float64\n"
                  "        return self.side * 4.0\n")
        self.assertEqual(codes(program(header=header)), ["E126"])

    def test_no_false_positives_on_rich_code(self):
        src = (
            "use Core\n"
            "fn largest[T](items: Vec(T)) -> T?\n"
            "    let mut best: T? = none\n"
            "    for it in items do\n"
            "        if best.is_none() or it > best.unwrap() then\n"
            "            best = Some(it)\n"
            "    return best\n"
            "fn twice(f: fn(int) -> int, x: int) -> int = f(f(x))\n"
            "fn main() -> int\n"
            "    let words = \"a bb ccc\".split(\" \")\n"
            "    let lens = words.map(w => w.len()).filter(n => n > 1)\n"
            "    let total = lens.fold(0, (acc, n) => acc + n)\n"
            "    let pairs = words.zip(lens)\n"
            "    for (w, n) in pairs do\n"
            "        println(f\"{w}={n}\")\n"
            "    let (a, b) = (1, \"x\")\n"
            "    println(largest([3, 1]), twice(x => x + 1, total), a, b)\n"
            "    let r: int!str = Ok(1)\n"
            "    println(r.map(x => x * 2).unwrap_or(0), r.ok().unwrap_or(0))\n"
            "    return 0\n")
        self.assertEqual(codes(src), [])


class InterpreterSemanticsTests(unittest.TestCase):
    def run_src(self, source: str, *args):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "main.ax").write_text(source, encoding="utf-8")
            return axon(["axc", "run", "--interpret", "main.ax", *args], tmp)

    def test_integer_overflow_panics(self):
        r = self.run_src(program("let big = 9223372036854775807", "println(big + 1)"))
        self.assertEqual(r.returncode, 101)
        self.assertIn("integer overflow", r.stderr)

    def test_c_style_division_and_modulo(self):
        r = self.run_src(program("println(-7 / 2, -7 % 3, 7 % -3, 10.0 % 3.0)"))
        self.assertEqual(r.stdout.strip(), "-3 -1 1 1.0")

    def test_deep_recursion(self):
        header = "fn depth(n: int) -> int\n    if n == 0 then\n        return 0\n    return 1 + depth(n - 1)\n"
        r = self.run_src(program("println(depth(20000))", header=header))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "20000")

    def test_runaway_recursion_is_a_panic(self):
        header = "fn forever(n: int) -> int\n    return forever(n + 1)\n"
        r = self.run_src(program("println(forever(0))", header=header))
        self.assertEqual(r.returncode, 101)
        self.assertIn("stack overflow", r.stderr)

    def test_defer_is_block_scoped(self):
        r = self.run_src(program(
            "if true then", "    defer println(\"inner\")", "    println(\"body\")",
            "println(\"after\")"))
        self.assertEqual(r.stdout.split(), ["body", "inner", "after"])

    def test_try_inside_closure_returns_from_closure(self):
        r = self.run_src(program(
            "let parse = fn(s: str) -> int!str = Ok(try s.parse(int))",
            "println(parse(\"4\"), parse(\"x\"))", "println(\"still running\")"))
        self.assertEqual(r.stdout.splitlines(), ["Ok(4) Err(parse error)", "still running"])

    def test_syntax_additions(self):
        r = self.run_src(program(
            "let (a, b) = (1, 2)",
            "let add = (x, y) => x + y",
            "let mut t = 0",
            "for i in 1..=4 do", "    t += i",
            'println(add(a, b), t, f"{Some(1).map(x => x * 3).unwrap_or(0)} {"q"}")'))
        self.assertEqual(r.stdout.strip(), "3 10 3 q")

    def test_process_api(self):
        r = self.run_src(program("println(Process.args())", "Process.exit(3)"), "x", "y")
        self.assertEqual(r.stdout.strip(), "[x, y]")
        self.assertEqual(r.returncode, 3)

    def test_trait_default_methods(self):
        header = ("trait Greet\n    fn name(self) -> str\n"
                  "    fn hello(self) -> str = f\"hello {self.name()}\"\n"
                  "struct U\n    n: str\n"
                  "impl U as Greet\n    fn name(self) -> str\n        return self.n\n")
        r = self.run_src(program('println(U{ n: "ada" }.hello())', header=header))
        self.assertEqual(r.stdout.strip(), "hello ada")


@unittest.skipUnless(HAVE_CC, "a C compiler is required for native parity tests")
class NativeFeatureParityTests(unittest.TestCase):
    def check(self, source: str, args=()):
        interp, native = run_both(source, args)
        self.assertEqual(interp[0], 0, interp[2])
        self.assertEqual(native[0], 0, native[2])
        self.assertEqual(interp[1], native[1])
        return native[1]

    def test_closures_and_higher_order_methods(self):
        out = self.check(program(
            "let nums = [5, 3, 8, 1]",
            "let factor = 10",
            "println(nums.map(n => n * factor), nums.filter(n => n % 2 == 1))",
            "println(nums.fold(0, (acc, n) => acc + n), nums.any(n => n > 7), nums.all(n => n > 1))",
            "println(nums.count(n => n > 2), nums.find(n => n > 4), nums.position(n => n == 8))",
            "println(nums.sort_by((a, b) => b - a))",
            "let add = fn(a: int, b: int) -> int = a + b",
            "println(add(2, 3), apply(double, 4), apply(x => x - 1, 1))",
            header="fn double(x: int) -> int = x * 2\n"
                   "fn apply(f: fn(int) -> int, x: int) -> int\n    return f(x)\n"))
        self.assertEqual(out.splitlines()[0], "[50, 30, 80, 10] [5, 3, 1]")

    def test_tuples_destructuring_and_map_iteration(self):
        out = self.check(program(
            "let t = (1, \"two\", 3.5)",
            "let (a, b, c) = t",
            "println(t, t.1, a, b, c)",
            "let mut ages: Map(str, int) = Map.new()",
            "ages.insert(\"ada\", 36)",
            "ages.insert(\"bob\", 20)",
            "for (name, age) in ages do",
            "    println(name, age)",
            "println(ages.entries(), [\"x\", \"y\"].enumerate(), [1, 2].zip([\"a\", \"b\"]))",
            "for ch in \"hé\" do",
            "    print(f\"<{ch}>\")",
            "println()"))
        self.assertEqual(out.splitlines()[0], "(1, two, 3.5) two 1 two 3.5")

    def test_generic_functions(self):
        out = self.check(program(
            "println(identity(5), identity(\"s\"), largest([3, 9, 2]), largest([\"b\", \"a\"]))",
            "println(apply_twice(x => x * 3, 2), apply_twice(s => s + \"!\", \"hey\"))",
            header="fn identity[T](x: T) -> T\n    return x\n"
                   "fn largest[T](items: Vec(T)) -> T?\n"
                   "    let mut best: T? = none\n"
                   "    for it in items do\n"
                   "        if best.is_none() or it > best.unwrap() then\n"
                   "            best = Some(it)\n"
                   "    return best\n"
                   "fn apply_twice[T](f: fn(T) -> T, x: T) -> T\n    return f(f(x))\n"))
        self.assertEqual(out.splitlines(), ["5 s Some(9) Some(b)", "18 hey!!"])

    def test_defer_on_every_exit(self):
        out = self.check(program(
            "println(parse(\"21\"), parse(\"x\"))",
            "let mut i = 0",
            "while i < 5 do",
            "    defer println(f\"iteration {i}\")",
            "    i += 1",
            "    if i == 2 then",
            "        continue",
            "    if i == 3 then",
            "        break",
            header="fn parse(s: str) -> int!str\n"
                   "    defer println(f\"done {s}\")\n"
                   "    let n = try s.parse(int)\n"
                   "    return Ok(n)\n"))
        self.assertEqual(out.splitlines(), [
            "done 21", "done x", "Ok(21) Err(parse error)",
            "iteration 1", "iteration 2", "iteration 3"])

    def test_option_result_combinators(self):
        self.check(program(
            "let o: int? = Some(4)",
            "let n: int? = none",
            "println(o.map(x => x + 1), o.and_then(x => if x > 3 then Some(x) else none))",
            "println(o.filter(x => x > 10), n.unwrap_or_else(() => 7), n.or(o), o.ok_or(\"no\"))",
            "let r: int!str = Err(\"bad\")",
            "let g: int!str = Ok(2)",
            "println(r.map(x => x * 2), r.map_err(e => f\"error: {e}\"), r.unwrap_or_else(e => e.len()))",
            "println(g.and_then(x => if x > 1 then Ok(x * 10) else Err(\"small\")), r.err(), g.err())"))

    def test_strings_and_vec_extras(self):
        self.check(program(
            "println(\"héllo\".len(), \"héllo\".chars().len(), \"a=b=c\".split_once(\"=\"))",
            "println(\"abc\".find(\"c\"), \"abc\".find(\"z\"), \"v1.2\".strip_prefix(\"v\"), \"x.ax\".strip_suffix(\".rs\"))",
            "let mut v = [3, 1, 2]",
            "v.insert(0, 9)",
            "v.extend([7, 7])",
            "let removed = v.remove(1)",
            "println(v, removed, v.index_of(7), v.min(), v.max(), v.take(2), v.skip(4))",
            "println([1, 1, 2, 2, 1].dedup(), [1, 2, 3, 4, 5].windows(2), [1, 2, 3, 4, 5].chunks(2))",
            "v.clear()",
            "println(v.is_empty(), (2.5).round(), (-2.5).round(), 7.is_odd(), (-3).signum())"))

    def test_left_to_right_evaluation(self):
        out = self.check(program(
            "println(note(\"a\"), note(\"b\"), note(\"c\"))",
            "let s = note(\"x\") + note(\"y\")",
            "println(s)",
            header="fn note(s: str) -> str\n    println(f\"eval {s}\")\n    return s\n"))
        self.assertEqual(out.splitlines()[:3], ["eval a", "eval b", "eval c"])

    def test_reserved_c_identifiers(self):
        self.check(program(
            "let int64_t = 2",
            "let floor = 3",
            "println(double(int64_t), floor, exit(1))",
            header="fn double(x: int) -> int = x * 2\n"
                   "fn exit(code: int) -> str = f\"code {code}\"\n"
                   "struct Unit\n    char: str\n"))

    def test_trait_defaults_optional_chaining_and_pipes(self):
        self.check(program(
            "let u = U{ n: \"ada\" }",
            "println(u.hello())",
            "let maybe: U? = Some(u)",
            "let missing: U? = none",
            "println(maybe?.n, missing?.n, maybe?.name())",
            "println(5 |> inc(), 5 |> inc() |> inc())",
            header="trait Greet\n    fn name(self) -> str\n"
                   "    fn hello(self) -> str = f\"hello {self.name()}\"\n"
                   "struct U\n    n: str\n"
                   "impl U as Greet\n    fn name(self) -> str\n        return self.n\n"
                   "fn inc(x: int) -> int = x + 1\n"))

    def test_float_formatting(self):
        self.check(program(
            "for x in [100.0, 0.1, 1.0 / 3.0, 1e20, 1.5e16, 0.0001, 0.00001, -0.0, 2.5e-7] do",
            "    println(x)",
            "println(0.0 / 0.0, 1.0 / 0.0, -1.0 / 0.0)"))

    def test_process_args_and_exit(self):
        interp, native = run_both(program("println(Process.args())", "Process.exit(4)"),
                                  args=("one", "two"))
        self.assertEqual(interp[:2], native[:2])
        self.assertEqual(native[:2], (4, "[one, two]\n"))

    def test_overflow_panics_in_debug_and_wraps_in_release(self):
        src = program("let big = 9223372036854775807", "println(big + 1)")
        interp, native = run_both(src)
        self.assertEqual(interp[0], 101)
        self.assertEqual(native[0], 101)
        self.assertEqual(interp[2].strip(), native[2].strip())
        _, release = run_both(src, release=True)
        self.assertEqual(release[:2], (0, "-9223372036854775808\n"))

    def test_new_examples_compile_natively(self):
        for name in ("12_inventory", "13_testing"):
            with self.subTest(example=name), tempfile.TemporaryDirectory() as tmp:
                out = pathlib.Path(tmp) / "app"
                compiled = axon(["axc", "compile", str(EXAMPLES / f"{name}.ax"), "-o", str(out)], tmp)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                self.assertIn("(native)", compiled.stdout)


class ToolingTests(unittest.TestCase):
    def test_test_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "m_test.ax").write_text(
                "use Core\n"
                "@test\nfn passes()\n    assert_eq(2 + 2, 4)\n"
                "@test\nfn fails()\n    assert_eq(2 + 2, 5)\n"
                "@test\nfn errs() -> int!str\n    return Err(\"nope\")\n"
                "@test\n@skip\nfn skipped()\n    panic(\"never\")\n",
                encoding="utf-8")
            r = axon(["axc", "test"], tmp)
            self.assertEqual(r.returncode, 1)
            self.assertIn("test passes ... ok", r.stdout)
            self.assertIn("test fails ... FAILED", r.stdout)
            self.assertIn("assert_eq failed: 4 != 5", r.stdout)
            self.assertIn("returned Err(nope)", r.stdout)
            self.assertIn("test skipped ... ignored", r.stdout)
            self.assertIn("1 passed; 2 failed; 1 ignored", r.stdout)

    def test_example_tests_pass(self):
        r = axon(["axc", "test", str(EXAMPLES / "13_testing.ax")], ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("3 passed; 0 failed", r.stdout)

    def test_new_project_tests_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(axon(["axbuild", "new", "demo"], tmp).returncode, 0)
            r = axon(["axbuild", "test"], pathlib.Path(tmp) / "demo")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("2 passed", r.stdout)

    def test_repl_keeps_bindings_and_definitions(self):
        script = ("let x = 5\n"
                  "x * 2\n"
                  "fn sq(n: int) -> int\n    return n * n\n\n"
                  "sq(x)\n"
                  "let mut t = 0\n"
                  "for i in 0..4 do\n    t += i\n\n"
                  "t\n")
        r = axon(["axrepl"], ROOT, stdin=script)
        self.assertEqual(r.stdout.splitlines()[1:], ["10", "defined sq", "25", "6"])

    def test_lints(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "l.ax").write_text(
                "use Core\nfn main() -> int\n    let unused = 1\n    let _ok = 2\n"
                "    return 0\n    println(\"never\")\n", encoding="utf-8")
            r = axon(["axlint", "l.ax"], tmp)
            self.assertIn("W020 unused variable `unused`", r.stderr)
            self.assertNotIn("`_ok`", r.stderr)
            self.assertIn("W021 unreachable statement", r.stderr)


if __name__ == "__main__":
    unittest.main()
