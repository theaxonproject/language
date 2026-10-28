"""Static type checking for AXON.

Runs after name resolution (`checker.py`) and before execution or code
generation, so `axc check` reports type errors without running or compiling
the program. The pass performs local, bidirectional type inference: declared
types (annotations, parameters, return types, struct fields) are pushed into
expressions, and everything else is synthesized bottom-up.

What it reports:

- E110 type mismatches (initializers, arguments, returns, operators, `if` and
  `match` arms, list elements, assignments)
- E111 wrong argument counts and unknown named arguments
- E103 unknown fields, E104 unknown methods (user types *and* builtins)
- E112 non-exhaustive `match` (Maranget-style usefulness on enums, Option,
  Result, bool, and tuples of those)
- E121 invalid `return`, E123 missing return, E124 `catch` blocks that fall
  through, E125 `break`/`continue` outside a loop
- E122 assignment to (or mutation of) an immutable binding
- E126 an `impl ... as Trait` missing required trait methods

Types the checker cannot see into (undeclared library types, generic type
parameters, dynamically shaped values such as JSON) are `UNKNOWN`, which is
compatible with everything. The pass therefore never rejects a program for
something it cannot prove wrong.

Type representation
-------------------
Plain strings for scalars: "int", "float", "bool", "str", "void", "never".
Tuples for everything else: ("struct", Name), ("enum", Name), ("opt", T),
("res", T, E), ("vec", T), ("map", K, V), ("tup", T...), ("fn", Ret,
Param...), ("range",), and ("type", Name) for a type used as a value
(`Point.new`, `Math.PI`).
"""

from __future__ import annotations

from . import ast
from .errors import Diagnostic

UNKNOWN = "?"

_INT_NAMES = {
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64", "byte", "rune",
}
_FLOAT_NAMES = {"float32", "float64"}
_DIVERGING_CALLS = {"panic", "todo", "unreachable", "unimplemented"}
_MUTATING_METHODS = {"push", "pop", "insert", "remove", "clear", "extend"}

INT, FLOAT, BOOL, STR, VOID, NEVER = "int", "float", "bool", "str", "void", "never"
RANGE = ("range",)


def show(t) -> str:
    """Render a type the way a user writes it."""
    if t == UNKNOWN:
        return "_"
    if t == FLOAT:
        return "float64"
    if isinstance(t, str):
        return t
    kind = t[0]
    if kind in {"struct", "enum", "type"}:
        return t[1]
    if kind == "opt":
        return f"{show(t[1])}?"
    if kind == "res":
        return f"{show(t[1])}!{show(t[2])}" if t[2] != UNKNOWN else f"{show(t[1])}!"
    if kind == "vec":
        return f"Vec({show(t[1])})"
    if kind == "map":
        return f"Map({show(t[1])}, {show(t[2])})"
    if kind == "tup":
        return "(" + ", ".join(show(e) for e in t[1:]) + ")"
    if kind == "fn":
        params = ", ".join(show(p) for p in t[2:])
        return f"fn({params}) -> {show(t[1])}"
    if kind == "range":
        return "Range"
    return str(t)


def compatible(actual, expected) -> bool:
    """Can a value of type `actual` be used where `expected` is required?"""
    if actual == UNKNOWN or expected == UNKNOWN or actual == NEVER:
        return True
    if actual == expected:
        return True
    if actual == INT and expected == FLOAT:
        return True  # integers widen to floats
    if isinstance(actual, tuple) and isinstance(expected, tuple):
        if actual[0] != expected[0] or len(actual) != len(expected):
            return False
        if actual[0] in {"struct", "enum", "type"}:
            return actual[1] == expected[1]
        if actual[0] == "fn":
            # parameter positions are checked the other way round
            return (compatible(actual[1], expected[1]) and
                    all(compatible(e, a) for a, e in zip(actual[2:], expected[2:])))
        return all(_same(a, e) for a, e in zip(actual[1:], expected[1:]))
    return False


def _same(a, b) -> bool:
    """Invariant compatibility for container element types."""
    if a == UNKNOWN or b == UNKNOWN or a == b:
        return True
    if isinstance(a, tuple) and isinstance(b, tuple):
        return compatible(a, b)
    return False


def _join(a, b):
    """The common type of two branches, or None when they disagree."""
    if a == NEVER:
        return b
    if b == NEVER:
        return a
    if a == UNKNOWN:
        return b if not _has_unknown(b) else a
    if b == UNKNOWN:
        return a
    if {a, b} == {INT, FLOAT}:
        return FLOAT
    if compatible(a, b):
        return b if _has_unknown(a) else a
    if compatible(b, a):
        return a if _has_unknown(b) else b
    return None


def _has_unknown(t) -> bool:
    if t == UNKNOWN:
        return True
    return isinstance(t, tuple) and any(_has_unknown(x) for x in t[1:])


def _is_numeric(t) -> bool:
    return t in (INT, FLOAT)


class _Var:
    __slots__ = ("type", "mutable")

    def __init__(self, type_, mutable: bool) -> None:
        self.type = type_
        self.mutable = mutable


class _FnCtx:
    def __init__(self, name: str, ret, self_type=None, self_mut: bool = False,
                 is_lambda: bool = False) -> None:
        self.name = name
        self.ret = ret
        self.self_type = self_type
        self.self_mut = self_mut
        self.is_lambda = is_lambda
        self.loop_depth = 0


class _Sig:
    def __init__(self, name: str, params: list, ret, has_self: bool = False,
                 self_mut: bool = False) -> None:
        self.name = name
        self.params = params  # list of (name, type, has_default, is_variadic)
        self.ret = ret
        self.has_self = has_self
        self.self_mut = self_mut

    def fn_type(self):
        return ("fn", self.ret, *[p[1] for p in self.params])


class TypeChecker:
    def __init__(self, module: ast.Module) -> None:
        self.module = module
        self.diagnostics: list[Diagnostic] = []
        self.structs: dict[str, ast.StructDef] = {}
        self.enums: dict[str, ast.EnumDef] = {}
        self.traits: dict[str, ast.TraitDef] = {}
        self.aliases: dict[str, ast.Node] = {}
        self.variant_owner: dict[str, str] = {}
        self.functions: dict[str, _Sig] = {}
        self.methods: dict[str, dict[str, _Sig]] = {}
        self.consts: dict[str, object] = {}
        self.scopes: list[dict[str, _Var]] = []
        self.fn: _FnCtx | None = None

    # -- diagnostics ----------------------------------------------------------
    def error(self, code: str, message: str, span=None, help_text=None) -> None:
        self.diagnostics.append(Diagnostic(code, message, span, help_text))

    def mismatch(self, expected, actual, span, what: str = "") -> None:
        prefix = f"{what}: " if what else ""
        self.error("E110", f"{prefix}expected `{show(expected)}`, found `{show(actual)}`", span)

    def expect(self, actual, expected, span, what: str = "") -> None:
        if not compatible(actual, expected):
            self.mismatch(expected, actual, span, what)

    # -- declarations ---------------------------------------------------------
    def check(self) -> list[Diagnostic]:
        self._collect()
        for item in self.module.items:
            if isinstance(item, ast.Const):
                t = self.expr(item.value, self.resolve(item.type) if item.type else None)
                if item.type is not None:
                    declared = self.resolve(item.type)
                    self.expect(t, declared, item.value.span, f"constant `{item.name}`")
                    t = declared
                self.consts[item.name] = t
        for item in self.module.items:
            if isinstance(item, ast.FnDef):
                self._check_fn(item, None)
            elif isinstance(item, ast.ImplBlock):
                self._check_impl(item)
            elif isinstance(item, ast.TraitDef):
                for m in item.methods:
                    if m.body is not None:
                        self._check_fn(m, item.name)
        return self.diagnostics

    def _collect(self) -> None:
        for item in self.module.items:
            if isinstance(item, ast.StructDef) and item.name != "__extern__":
                self.structs[item.name] = item
            elif isinstance(item, ast.EnumDef):
                self.enums[item.name] = item
                for v in item.variants:
                    self.variant_owner.setdefault(v.name, item.name)
            elif isinstance(item, ast.TraitDef):
                self.traits[item.name] = item
            elif isinstance(item, ast.TypeAlias):
                self.aliases[item.name] = item.aliased
        for item in self.module.items:
            if isinstance(item, ast.FnDef):
                self.functions[item.name] = self._sig(item, None)
            elif isinstance(item, ast.ImplBlock):
                table = self.methods.setdefault(item.type_name, {})
                for m in item.methods:
                    table[m.name] = self._sig(m, item.type_name, item.generics)
        # trait default methods become methods of every implementing type
        for item in self.module.items:
            if isinstance(item, ast.ImplBlock) and item.trait_name in self.traits:
                table = self.methods.setdefault(item.type_name, {})
                for m in self.traits[item.trait_name].methods:
                    if m.body is not None and m.name not in table:
                        table[m.name] = self._sig(m, item.type_name)

    def _sig(self, fn: ast.FnDef, owner: str | None, extra_generics=()) -> _Sig:
        generics = set(fn.generics) | set(extra_generics)
        if owner in self.structs:
            generics |= set(self.structs[owner].generics)
        if owner in self.enums:
            generics |= set(self.enums[owner].generics)
        params = []
        has_self = self_mut = False
        for p in fn.params:
            if p.is_self:
                has_self, self_mut = True, p.is_mut
                continue
            t = self.resolve(p.type, generics) if p.type is not None else UNKNOWN
            if p.is_variadic:
                t = UNKNOWN
            params.append((p.name, t, p.default is not None, p.is_variadic))
        if fn.return_type is not None:
            ret = self.resolve(fn.return_type, generics)
        elif self._expression_bodied(fn):
            ret = UNKNOWN  # `fn f(x: int) = x * 2` infers its return type
        else:
            ret = VOID
        return _Sig(fn.name, params, ret, has_self, self_mut)

    @staticmethod
    def _expression_bodied(fn: ast.FnDef) -> bool:
        body = fn.body
        return (body is not None and len(body.stmts) == 1
                and isinstance(body.stmts[0], ast.ReturnStmt)
                and body.stmts[0].value is not None
                and body.stmts[0].span is body.stmts[0].value.span)

    def resolve(self, node, generics=frozenset()):
        """Turn a type expression into a checker type."""
        if node is None:
            return VOID
        if isinstance(node, ast.TypeName):
            n = node.name
            if n in generics:
                return UNKNOWN
            if n in _INT_NAMES:
                return INT
            if n in _FLOAT_NAMES or n == "float":
                return FLOAT
            if n in {"bool", "str", "void", "never"}:
                return n
            if n in self.aliases:
                return self.resolve(self.aliases[n], generics)
            if n in self.structs:
                return ("struct", n)
            if n in self.enums:
                return ("enum", n)
            args = [self.resolve(a, generics) for a in node.args]
            if n == "Option" and len(args) == 1:
                return ("opt", args[0])
            if n == "Result" and args:
                return ("res", args[0], args[1] if len(args) > 1 else UNKNOWN)
            if n == "Vec" and len(args) == 1:
                return ("vec", args[0])
            if n == "Map" and len(args) == 2:
                return ("map", args[0], args[1])
            return UNKNOWN
        if isinstance(node, ast.OptionalType):
            return ("opt", self.resolve(node.inner, generics))
        if isinstance(node, ast.ResultType):
            err = self.resolve(node.err, generics) if node.err is not None else UNKNOWN
            return ("res", self.resolve(node.ok, generics), err)
        if isinstance(node, ast.RefType):
            return self.resolve(node.inner, generics)
        if isinstance(node, (ast.ArrayType, ast.SliceType)):
            return ("vec", self.resolve(node.elem, generics))
        if isinstance(node, ast.TupleType):
            if not node.elems:
                return VOID
            return ("tup", *[self.resolve(e, generics) for e in node.elems])
        if isinstance(node, ast.FnType):
            ret = self.resolve(node.ret, generics) if node.ret is not None else VOID
            return ("fn", ret, *[self.resolve(p, generics) for p in node.params])
        return UNKNOWN

    def _field_types(self, struct_name: str) -> dict[str, object]:
        sdef = self.structs[struct_name]
        generics = set(sdef.generics)
        return {f.name: self.resolve(f.type, generics) for f in sdef.fields}

    def _payload_types(self, enum_name: str, variant: ast.Variant) -> list:
        generics = set(self.enums[enum_name].generics)
        return [self.resolve(t, generics) for _, t in variant.payload]

    def _variant(self, enum_name: str, name: str):
        return next((v for v in self.enums[enum_name].variants if v.name == name), None)

    # -- functions ------------------------------------------------------------
    def _check_impl(self, impl: ast.ImplBlock) -> None:
        if impl.trait_name in self.traits:
            provided = {m.name for m in impl.methods}
            for req in self.traits[impl.trait_name].methods:
                if req.body is None and req.name not in provided:
                    self.error(
                        "E126",
                        f"`{impl.type_name}` does not implement `{req.name}`, "
                        f"required by trait `{impl.trait_name}`",
                        impl.span,
                    )
        for m in impl.methods:
            self._check_fn(m, impl.type_name, impl.generics)

    def _check_fn(self, fn: ast.FnDef, owner: str | None, extra_generics=()) -> None:
        if fn.body is None:
            return
        sig = (self.methods.get(owner, {}).get(fn.name) if owner else self.functions.get(fn.name))
        if sig is None or (owner and fn.owner != owner):
            sig = self._sig(fn, owner, extra_generics)
        self_type = None
        if owner in self.structs:
            self_type = ("struct", owner)
        elif owner in self.enums:
            self_type = ("enum", owner)
        elif owner is not None:
            self_type = UNKNOWN
        outer_fn, outer_scopes = self.fn, self.scopes
        self.fn = _FnCtx(fn.name, sig.ret, self_type, sig.self_mut)
        params = {}
        explicit = {p.name: p for p in fn.params if not p.is_self}
        for name, t, _, variadic in sig.params:
            params[name] = _Var(("vec", UNKNOWN) if variadic else t, explicit[name].is_mut)
        self.scopes = [params]
        for name, t, has_default, _ in sig.params:
            p = explicit[name]
            if has_default and p.default is not None:
                self.expect(self.expr(p.default, t), t, p.default.span,
                            f"default for parameter `{name}`")
        self.block(fn.body)
        if (sig.ret not in (VOID, UNKNOWN, NEVER) and fn.name != "main"
                and not self._terminates(fn.body)):
            self.error(
                "E123",
                f"function `{fn.name}` can reach its end without returning a `{show(sig.ret)}`",
                fn.span,
                "add a `return` at the end, or make every branch return",
            )
        elif sig.ret == INT and fn.name == "main" and not self._terminates(fn.body):
            self.error("E123", "`main` declares `-> int` but can reach its end without "
                       "returning an exit status", fn.span,
                       "end `main` with `return 0`")
        self.fn, self.scopes = outer_fn, outer_scopes

    # -- scopes ---------------------------------------------------------------
    def push(self) -> None:
        self.scopes.append({})

    def pop(self) -> None:
        self.scopes.pop()

    def bind(self, name: str, t, mutable: bool = False) -> None:
        if name and name != "_":
            self.scopes[-1][name] = _Var(t, mutable)

    def lookup(self, name: str) -> _Var | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    # -- statements -----------------------------------------------------------
    def block(self, block: ast.Block, bindings: dict | None = None,
              want_value: bool = False):
        """Check a block in a fresh scope. With `want_value`, return the type
        of its trailing expression statement (the block's value)."""
        self.push()
        for name, (t, mutable) in (bindings or {}).items():
            self.bind(name, t, mutable)
        result = VOID
        for i, stmt in enumerate(block.stmts):
            last = i == len(block.stmts) - 1
            if want_value and last and isinstance(stmt, ast.ExprStmt):
                result = self.expr(stmt.expr)
            else:
                self.stmt(stmt)
                if self._stmt_terminates(stmt):
                    result = NEVER
        self.pop()
        return result

    def stmt(self, s: ast.Node) -> None:
        k = type(s)
        if k is ast.LetStmt:
            self._let(s)
        elif k is ast.AssignStmt:
            self._assign(s)
        elif k is ast.ReturnStmt:
            self._return(s)
        elif k is ast.ExprStmt:
            e = s.expr
            if isinstance(e, ast.MatchExpr):
                self._match(e, None, want_value=False)
            elif isinstance(e, ast.IfExpr):
                self.expr(e.cond)
                self.expr(e.then)
                self.expr(e.els)
            else:
                self.expr(e)
        elif k is ast.DeferStmt:
            self.expr(s.expr)
        elif k is ast.IfStmt:
            self.expr(s.cond)
            self.block(s.then)
            for cond, blk in s.elifs:
                self.expr(cond)
                self.block(blk)
            if s.els is not None:
                self.block(s.els)
        elif k is ast.IfLetStmt:
            t = self.expr(s.expr)
            binds: dict = {}
            self.pattern(s.pattern, t, binds)
            self.block(s.then, binds)
            if s.els is not None:
                self.block(s.els)
        elif k is ast.ForStmt:
            elem = self._iter_elem(self.expr(s.iterable), s.iterable.span)
            binds = {}
            self.pattern(s.pattern, elem, binds)
            self._loop(s.body, binds)
        elif k is ast.WhileStmt:
            self.expr(s.cond)
            self._loop(s.body)
        elif k is ast.LoopStmt:
            self._loop(s.body)
        elif k in (ast.BreakStmt, ast.ContinueStmt):
            if self.fn is not None and self.fn.loop_depth == 0:
                word = "break" if k is ast.BreakStmt else "continue"
                self.error("E125", f"`{word}` outside of a loop", s.span)
        elif k is ast.TaskStmt:
            self.expr(s.expr)
        elif k is ast.TaskGroupStmt:
            self.block(s.body)

    def _loop(self, body: ast.Block, binds=None) -> None:
        self.fn.loop_depth += 1
        self.block(body, binds)
        self.fn.loop_depth -= 1

    def _iter_elem(self, t, span):
        if t == UNKNOWN:
            return UNKNOWN
        if t == RANGE:
            return INT
        if isinstance(t, tuple) and t[0] == "vec":
            return t[1]
        if isinstance(t, tuple) and t[0] == "map":
            return ("tup", t[1], t[2])
        if t == STR:
            return STR
        if isinstance(t, tuple) and t[0] in {"type", "struct", "enum"}:
            return UNKNOWN  # library iterables (channels, streams)
        self.error("E110", f"`{show(t)}` is not iterable", span,
                   "`for` iterates over ranges, Vec, Map, and str values")
        return UNKNOWN

    def _let(self, s: ast.LetStmt) -> None:
        declared = self.resolve(s.type) if s.type is not None else None
        if s.catch_block is not None:
            inner = s.value.expr if isinstance(s.value, ast.TryExpr) else s.value
            t = self.expr(inner)
            ok_t, err_t = self._unwrap_fallible(t)
            self.block(s.catch_block, {s.catch_name: (err_t, False)} if s.catch_name else None)
            if not self._terminates(s.catch_block):
                self.error(
                    "E124",
                    f"the `catch` block for `{s.name}` must exit (return, break, "
                    f"continue, or panic)",
                    s.span,
                    "otherwise the binding would be used without a value",
                )
            if declared is not None:
                self.expect(ok_t, declared, s.span, f"`{s.name}`")
                ok_t = declared
            self.bind(s.name, ok_t, s.is_mut)
            return
        if s.value is None:
            # `let x: T` declares now and initializes later, so it is assignable
            self.bind(s.name, declared if declared is not None else UNKNOWN, True)
            return
        t = self.expr(s.value, declared)
        if declared is not None:
            if not compatible(t, declared):
                self.error("E110", f"`{s.name}` is declared as `{show(declared)}` but "
                           f"initialized with `{show(t)}`", s.value.span)
            t = declared
        elif t == VOID:
            self.error("E110", f"`{s.name}` is initialized with an expression that has "
                       f"no value", s.value.span)
            t = UNKNOWN
        if s.pattern is not None:
            binds: dict = {}
            self.pattern(s.pattern, t, binds)
            for name, (bt, _) in binds.items():
                self.bind(name, bt, s.is_mut)
            return
        self.bind(s.name, t, s.is_mut)

    def _unwrap_fallible(self, t):
        if isinstance(t, tuple) and t[0] == "res":
            return t[1], t[2]
        if isinstance(t, tuple) and t[0] == "opt":
            return t[1], t
        return UNKNOWN, UNKNOWN

    def _assign(self, s: ast.AssignStmt) -> None:
        target_t = self.expr(s.target)
        self._require_mutable(s.target, s.span)
        if s.op == "=":
            value_t = self.expr(s.value, target_t)
            self.expect(value_t, target_t, s.value.span, "assignment")
            return
        value_t = self.expr(s.value, target_t)
        result = self._arith(s.op[:-1], target_t, value_t, s.span)
        self.expect(result, target_t, s.span, f"`{s.op}`")

    def _require_mutable(self, target, span, via_method: str | None = None) -> None:
        root = target
        while isinstance(root, (ast.FieldExpr, ast.IndexExpr)):
            root = root.recv
        if isinstance(root, ast.SelfExpr):
            if self.fn is not None and self.fn.self_type is not None and not self.fn.self_mut:
                action = f"call `{via_method}` on" if via_method else "assign to"
                self.error("E122", f"cannot {action} `self` in a method that takes "
                           "immutable `self`", span,
                           "declare the method with `mut self`")
            return
        if not isinstance(root, ast.NameExpr):
            return
        var = self.lookup(root.name)
        if var is None or var.mutable:
            return
        if via_method is not None:
            self.error("E122", f"cannot call `{via_method}` on immutable binding "
                       f"`{root.name}`", span, f"declare it with `let mut {root.name}`")
        elif root is target:
            self.error("E122", f"cannot assign twice to immutable binding `{root.name}`",
                       span, f"declare it with `let mut {root.name}`")
        else:
            self.error("E122", f"cannot assign through immutable binding `{root.name}`",
                       span, f"declare it with `let mut {root.name}`")

    def _return(self, s: ast.ReturnStmt) -> None:
        fn = self.fn
        if fn is None:
            return
        if fn.is_lambda:
            if s.value is not None:
                self.expr(s.value)
            return
        if s.value is None:
            if fn.ret not in (VOID, UNKNOWN):
                self.error("E121", f"`{fn.name}` must return a `{show(fn.ret)}`", s.span)
            return
        t = self.expr(s.value, fn.ret)
        if fn.ret == VOID:
            self.error("E121", f"`{fn.name}` has no return type but returns a value",
                       s.value.span,
                       f"declare the return type, e.g. `-> {show(t) if t != UNKNOWN else 'int'}`")
            return
        self.expect(t, fn.ret, s.value.span, f"return value of `{fn.name}`")

    # -- termination ----------------------------------------------------------
    def _terminates(self, block: ast.Block) -> bool:
        return any(self._stmt_terminates(s) for s in block.stmts)

    def _stmt_terminates(self, s) -> bool:
        k = type(s)
        if k in (ast.ReturnStmt, ast.BreakStmt, ast.ContinueStmt):
            return True
        if k is ast.ExprStmt:
            return self._expr_diverges(s.expr)
        if k is ast.IfStmt:
            return (s.els is not None and self._terminates(s.then)
                    and all(self._terminates(b) for _, b in s.elifs)
                    and self._terminates(s.els))
        if k is ast.IfLetStmt:
            return s.els is not None and self._terminates(s.then) and self._terminates(s.els)
        if k is ast.LoopStmt:
            return not self._has_break(s.body)
        if k is ast.WhileStmt:
            return (isinstance(s.cond, ast.BoolLit) and s.cond.value
                    and not self._has_break(s.body))
        return False

    def _expr_diverges(self, e) -> bool:
        if isinstance(e, ast.CallExpr):
            c = e.callee
            if isinstance(c, ast.NameExpr) and c.name in _DIVERGING_CALLS:
                return self.lookup(c.name) is None
            if (isinstance(c, ast.FieldExpr) and isinstance(c.recv, ast.NameExpr)
                    and c.recv.name == "Process" and c.name == "exit"):
                return True
        if isinstance(e, ast.MatchExpr):
            return bool(e.arms) and all(
                self._terminates(arm.body) for arm in e.arms
            ) and getattr(e, "_exhaustive", True)
        return False

    def _has_break(self, block: ast.Block) -> bool:
        for s in block.stmts:
            k = type(s)
            if k is ast.BreakStmt:
                return True
            if k is ast.IfStmt:
                if self._has_break(s.then) or any(self._has_break(b) for _, b in s.elifs):
                    return True
                if s.els is not None and self._has_break(s.els):
                    return True
            elif k is ast.IfLetStmt:
                if self._has_break(s.then) or (s.els is not None and self._has_break(s.els)):
                    return True
            elif k is ast.ExprStmt and isinstance(s.expr, ast.MatchExpr):
                if any(self._has_break(arm.body) for arm in s.expr.arms):
                    return True
            elif k is ast.TaskGroupStmt and self._has_break(s.body):
                return True
        return False

    # -- expressions ----------------------------------------------------------
    def expr(self, e, expected=None):
        t = self._expr(e, expected)
        return UNKNOWN if t is None else t

    def _expr(self, e, expected):
        k = type(e)
        if k is ast.IntLit:
            if e.suffix and e.suffix.startswith("f"):
                return FLOAT
            return FLOAT if expected == FLOAT else INT
        if k is ast.FloatLit:
            return FLOAT
        if k is ast.BoolLit:
            return BOOL
        if k in (ast.StrLit, ast.CharLit):
            return STR
        if k is ast.FStr:
            for part in e.parts:
                if not isinstance(part, str):
                    t = self.expr(part)
                    if t == VOID:
                        self.error("E110", "this interpolated expression has no value",
                                   part.span)
            return STR
        if k is ast.NoneLit:
            if isinstance(expected, tuple) and expected[0] == "opt":
                return expected
            return ("opt", UNKNOWN)
        if k is ast.NameExpr:
            return self._name(e)
        if k is ast.SelfExpr:
            return self.fn.self_type if self.fn and self.fn.self_type else UNKNOWN
        if k is ast.BinaryExpr:
            return self._binary(e)
        if k is ast.UnaryExpr:
            return self._unary(e)
        if k is ast.RefExpr:
            return self.expr(e.operand, expected)
        if k is ast.CallExpr:
            return self._call(e, expected)
        if k is ast.FieldExpr:
            return self._field(e)
        if k is ast.OptFieldExpr:
            inner = self.expr(e.recv)
            if isinstance(inner, tuple) and inner[0] == "opt":
                inner = inner[1]
            ft = self._field_of(inner, e.name, e.span)
            return ("opt", ft)
        if k is ast.IndexExpr:
            return self._index(e)
        if k is ast.RangeExpr:
            for part in (e.start, e.end):
                if part is not None:
                    self.expect(self.expr(part, INT), INT, part.span, "range bound")
            return RANGE
        if k is ast.ListLit:
            return self._list(e, expected)
        if k is ast.ArrayRepeat:
            elem_expect = expected[1] if isinstance(expected, tuple) and expected[0] == "vec" else None
            t = self.expr(e.value, elem_expect)
            self.expect(self.expr(e.count, INT), INT, e.count.span, "repeat count")
            return ("vec", t)
        if k is ast.TupleLit:
            if not e.elems:
                return VOID
            exp = expected if (isinstance(expected, tuple) and expected[0] == "tup"
                               and len(expected) == len(e.elems) + 1) else None
            return ("tup", *[self.expr(x, exp[i + 1] if exp else None)
                             for i, x in enumerate(e.elems)])
        if k is ast.RecordLit:
            return self._record(e)
        if k is ast.IfExpr:
            self.expr(e.cond)
            a = self.expr(e.then, expected)
            b = self.expr(e.els, expected if expected is not None else a)
            joined = _join(a, b)
            if joined is None:
                if isinstance(e.els, ast.NoneLit) or a == VOID or b == VOID:
                    return VOID
                self.error("E110", f"`if` branches have incompatible types "
                           f"`{show(a)}` and `{show(b)}`", e.span)
                return UNKNOWN
            return joined
        if k is ast.MatchExpr:
            return self._match(e, expected)
        if k is ast.LambdaExpr:
            return self._lambda(e, expected)
        if k is ast.TryExpr:
            return self._try(e, expected)
        if k is ast.PipeExpr:
            return self._pipe(e)
        if k is ast.CastExpr:
            return self._cast(e)
        return UNKNOWN

    def _name(self, e: ast.NameExpr):
        name = e.name
        var = self.lookup(name)
        if var is not None:
            return var.type
        if name in self.consts:
            return self.consts[name]
        if name in self.functions:
            return self.functions[name].fn_type()
        if name in self.variant_owner:
            owner = self.variant_owner[name]
            v = self._variant(owner, name)
            if v.payload:
                return ("fn", ("enum", owner), *self._payload_types(owner, v))
            return ("enum", owner)
        if name in self.structs or name in self.enums:
            return ("type", name)
        return UNKNOWN

    # -- operators ------------------------------------------------------------
    def _binary(self, e: ast.BinaryExpr):
        op = e.op
        if op in {"and", "or"}:
            a, b = self.expr(e.left), self.expr(e.right)
            return BOOL if a in (BOOL, UNKNOWN) and b in (BOOL, UNKNOWN) else UNKNOWN
        if op in {"==", "!="} and isinstance(e.right, ast.NoneLit):
            self.expr(e.left)
            return BOOL
        left = self.expr(e.left)
        right = self.expr(e.right, left if left != UNKNOWN else None)
        if op in {"==", "!="}:
            if not (compatible(left, right) or compatible(right, left)):
                self.error("E110", f"cannot compare `{show(left)}` with `{show(right)}`", e.span)
            return BOOL
        if op in {"<", "<=", ">", ">="}:
            ok_types = (_is_numeric(left) and _is_numeric(right)) or (left == right == STR)
            if not ok_types and UNKNOWN not in (left, right):
                if left == right and isinstance(left, tuple) and left[0] in {"vec", "tup", "opt"}:
                    return BOOL  # element-wise ordering
                self.error("E110", f"cannot order `{show(left)}` and `{show(right)}`", e.span)
            return BOOL
        if op == "is":
            return BOOL
        return self._arith(op, left, right, e.span)

    def _arith(self, op: str, left, right, span):
        if op in {"&", "|", "^"} and left == right == BOOL:
            return BOOL
        if op in {"<<", ">>", "&", "|", "^"}:
            for t in (left, right):
                if t not in (INT, UNKNOWN):
                    self.error("E110", f"`{op}` requires integer operands, found `{show(t)}`", span)
                    return UNKNOWN
            return INT
        if left == UNKNOWN or right == UNKNOWN:
            if op == "+" and STR in (left, right):
                return STR
            known = right if left == UNKNOWN else left
            return known if _is_numeric(known) else UNKNOWN
        if op == "+" and left == right == STR:
            return STR
        if op == "*" and left == STR and right == INT:
            return STR
        if op == "+" and left == right and isinstance(left, tuple) and left[0] == "vec":
            return left
        if _is_numeric(left) and _is_numeric(right):
            return FLOAT if FLOAT in (left, right) else INT
        hint = None
        if op == "+" and STR in (left, right):
            hint = "use an f-string (f\"{a}{b}\") or `.to_string()` to build text"
        self.error("E110", f"cannot apply `{op}` to `{show(left)}` and `{show(right)}`",
                   span, hint)
        return UNKNOWN

    def _unary(self, e: ast.UnaryExpr):
        t = self.expr(e.operand)
        if e.op == "not":
            return BOOL
        if e.op in {"-", "+"}:
            if t not in (INT, FLOAT, UNKNOWN):
                self.error("E110", f"cannot negate a value of type `{show(t)}`", e.span)
                return UNKNOWN
            return t
        if e.op == "~":
            if t not in (INT, UNKNOWN):
                self.error("E110", f"`~` requires an integer, found `{show(t)}`", e.span)
            return INT
        return t

    def _cast(self, e: ast.CastExpr):
        src = self.expr(e.expr)
        dst = self.resolve(e.type)
        if dst in (INT, FLOAT) and src == STR:
            self.error("E110", f"cannot cast `str` to `{show(dst)}`", e.span,
                       f"parse it instead: `.parse({show(dst)})`")
        elif dst in (INT, FLOAT) and src not in (INT, FLOAT, BOOL, UNKNOWN):
            if not (isinstance(src, tuple) and src[0] == "enum"):
                self.error("E110", f"cannot cast `{show(src)}` to `{show(dst)}`", e.span)
        return dst

    # -- collections ----------------------------------------------------------
    def _list(self, e: ast.ListLit, expected):
        elem = expected[1] if isinstance(expected, tuple) and expected[0] == "vec" else None
        if not e.elems:
            return ("vec", elem if elem is not None else UNKNOWN)
        first = self.expr(e.elems[0], elem)
        common = elem if elem is not None else first
        if elem is not None:
            self.expect(first, elem, e.elems[0].span, "list element")
        for x in e.elems[1:]:
            t = self.expr(x, common)
            joined = _join(common, t)
            if joined is None:
                self.error("E110", f"list elements must share a type: expected "
                           f"`{show(common)}`, found `{show(t)}`", x.span)
            elif elem is None:
                common = joined
        return ("vec", common)

    def _record(self, e: ast.RecordLit):
        if e.type_name not in self.structs:
            for _, value in e.fields:
                self.expr(value)
            return UNKNOWN
        fields = self._field_types(e.type_name)
        for fname, value in e.fields:
            ft = fields.get(fname)
            t = self.expr(value, ft)
            if ft is not None:
                self.expect(t, ft, value.span, f"field `{fname}` of `{e.type_name}`")
        return ("struct", e.type_name)

    def _index(self, e: ast.IndexExpr):
        recv = self.expr(e.recv)
        is_range = isinstance(e.index, ast.RangeExpr)
        if isinstance(recv, tuple) and recv[0] == "map":
            self.expect(self.expr(e.index, recv[1]), recv[1], e.index.span, "map key")
            return recv[2]
        idx = self.expr(e.index, INT)
        if recv == UNKNOWN:
            return UNKNOWN
        if not is_range and idx not in (INT, UNKNOWN):
            self.error("E110", f"index must be an `int`, found `{show(idx)}`", e.index.span)
        if recv == STR:
            return STR
        if isinstance(recv, tuple) and recv[0] == "vec":
            return recv if is_range else recv[1]
        if isinstance(recv, tuple) and recv[0] == "tup":
            return UNKNOWN
        if isinstance(recv, tuple) and recv[0] in {"struct", "enum", "type"}:
            return UNKNOWN
        self.error("E110", f"cannot index a value of type `{show(recv)}`", e.span)
        return UNKNOWN

    # -- fields ---------------------------------------------------------------
    def _field(self, e: ast.FieldExpr):
        recv = self.expr(e.recv)
        return self._field_of(recv, e.name, e.span)

    def _field_of(self, recv, name: str, span):
        if recv == UNKNOWN:
            return UNKNOWN
        if isinstance(recv, tuple) and recv[0] == "struct":
            fields = self._field_types(recv[1])
            if name in fields:
                return fields[name]
            sig = self.methods.get(recv[1], {}).get(name)
            if sig is not None:
                return sig.fn_type()
            self.error("E103", f"`{recv[1]}` has no field `{name}`", span)
            return UNKNOWN
        if isinstance(recv, tuple) and recv[0] == "tup":
            if name.isdigit():
                i = int(name)
                if i + 1 < len(recv):
                    return recv[i + 1]
                self.error("E103", f"tuple `{show(recv)}` has no field `{name}`", span)
            return UNKNOWN
        if isinstance(recv, tuple) and recv[0] == "type":
            return self._static_field(recv[1], name, span)
        if isinstance(recv, tuple) and recv[0] == "enum":
            return UNKNOWN
        if recv in (INT, FLOAT, BOOL, STR, VOID) or (
                isinstance(recv, tuple) and recv[0] in {"vec", "map", "opt", "res", "range"}):
            help_text = None
            if self._builtin_has_method(recv, name):
                help_text = f"`{name}` is a method; call it as `.{name}()`"
            self.error("E103", f"`{show(recv)}` has no field `{name}`", span, help_text)
        return UNKNOWN

    def _static_field(self, type_name: str, name: str, span):
        if type_name in self.enums:
            v = self._variant(type_name, name)
            if v is not None:
                if v.payload:
                    return ("fn", ("enum", type_name), *self._payload_types(type_name, v))
                return ("enum", type_name)
        sig = self.methods.get(type_name, {}).get(name)
        if sig is not None:
            return sig.fn_type()
        if type_name in self.structs or type_name in self.enums:
            self.error("E104", f"`{type_name}` has no associated item `{name}`", span)
        return UNKNOWN

    # -- calls ----------------------------------------------------------------
    def _call(self, e: ast.CallExpr, expected):
        callee = e.callee
        if isinstance(callee, ast.FieldExpr):
            return self._method_call(callee, e.args, expected, e.span)
        if isinstance(callee, ast.OptFieldExpr):
            recv = self.expr(callee.recv)
            inner = recv[1] if isinstance(recv, tuple) and recv[0] == "opt" else recv
            fake = ast.FieldExpr(callee.span, ast.NameExpr(callee.span, "__optrecv"), callee.name)
            self.push()
            self.bind("__optrecv", inner, True)
            t = self._method_call(fake, e.args, None, e.span)
            self.pop()
            return ("opt", t)
        if isinstance(callee, ast.NameExpr):
            name = callee.name
            var = self.lookup(name)
            if var is not None:
                return self._call_value(var.type, e.args, name, e.span)
            if name in self.functions:
                return self._call_sig(self.functions[name], e.args, e.span, name)
            builtin = self._builtin_call(name, e.args, expected, e.span)
            if builtin is not None:
                return builtin
            if name in self.variant_owner:
                return self._variant_ctor(self.variant_owner[name], name, e.args, e.span)
            self._args_unchecked(e.args)
            return UNKNOWN
        t = self.expr(callee)
        return self._call_value(t, e.args, None, e.span)

    def _args_unchecked(self, args) -> None:
        for a in args:
            self.expr(a.value)

    def _call_value(self, t, args, name, span):
        if isinstance(t, tuple) and t[0] == "fn":
            params = list(t[2:])
            if len(args) != len(params):
                label = f"`{name}`" if name else "this function"
                self.error("E111", f"{label} takes {len(params)} argument"
                           f"{'s' if len(params) != 1 else ''} but {len(args)} "
                           f"{'were' if len(args) != 1 else 'was'} given", span)
                self._args_unchecked(args)
                return t[1]
            for a, pt in zip(args, params):
                self.expect(self.expr(a.value, pt), pt, a.value.span, "argument")
            return t[1]
        if t not in (UNKNOWN,) and not (isinstance(t, tuple) and t[0] == "type"):
            label = f"`{name}`" if name else "this value"
            self.error("E110", f"{label} of type `{show(t)}` is not callable", span)
        self._args_unchecked(args)
        return UNKNOWN

    def _call_sig(self, sig: _Sig, args, span, label: str):
        params = sig.params
        positional = [a for a in args if a.name is None]
        named = {a.name: a for a in args if a.name is not None}
        variadic = any(p[3] for p in params)
        fixed = [p for p in params if not p[3]]
        if len(positional) > len(fixed) and not variadic:
            self.error("E111", f"`{label}` takes {len(fixed)} argument"
                       f"{'s' if len(fixed) != 1 else ''} but {len(positional)} "
                       f"{'were' if len(positional) != 1 else 'was'} given", span)
        pi = 0
        for pname, ptype, has_default, is_variadic in params:
            if is_variadic:
                for a in positional[pi:]:
                    self.expr(a.value)
                pi = len(positional)
                continue
            if pi < len(positional):
                a = positional[pi]
                pi += 1
                self.expect(self.expr(a.value, ptype), ptype, a.value.span,
                            f"argument `{pname}` of `{label}`")
            elif pname in named:
                a = named.pop(pname)
                self.expect(self.expr(a.value, ptype), ptype, a.value.span,
                            f"argument `{pname}` of `{label}`")
            elif not has_default:
                self.error("E111", f"missing argument `{pname}` in call to `{label}`", span)
        for a in positional[pi:]:
            self.expr(a.value)
        for a in named.values():
            self.expr(a.value)
            self.error("E111", f"`{label}` has no parameter named `{a.name}`", a.span)
        return sig.ret

    def _variant_ctor(self, owner: str, variant: str, args, span):
        v = self._variant(owner, variant)
        types = self._payload_types(owner, v)
        if not v.payload:
            self.error("E111", f"variant `{variant}` takes no arguments", span)
            self._args_unchecked(args)
            return ("enum", owner)
        names = [n for n, _ in v.payload]
        if len(args) != len(types):
            self.error("E111", f"variant `{variant}` takes {len(types)} argument"
                       f"{'s' if len(types) != 1 else ''} but {len(args)} "
                       f"{'were' if len(args) != 1 else 'was'} given", span)
            self._args_unchecked(args)
            return ("enum", owner)
        for i, a in enumerate(args):
            slot = names.index(a.name) if a.name in names else i
            self.expect(self.expr(a.value, types[slot]), types[slot], a.value.span,
                        f"payload of `{variant}`")
        return ("enum", owner)

    def _builtin_call(self, name: str, args, expected, span):
        """Free builtin functions. Returns None for names that are not builtins."""
        if name in {"println", "print", "eprintln", "eprint"}:
            for a in args:
                if self.expr(a.value) == VOID:
                    self.error("E110", "cannot print an expression that has no value",
                               a.value.span)
            return VOID
        if name in _DIVERGING_CALLS:
            self._args_unchecked(args)
            return NEVER
        if name == "assert":
            self._args_unchecked(args)
            return VOID
        if name == "assert_eq":
            if len(args) >= 2:
                a = self.expr(args[0].value)
                b = self.expr(args[1].value, a)
                if not (compatible(a, b) or compatible(b, a)):
                    self.error("E110", f"`assert_eq` compares `{show(a)}` with `{show(b)}`", span)
                self._args_unchecked(args[2:])
            else:
                self.error("E111", "`assert_eq` takes 2 arguments", span)
            return VOID
        if name == "read_line":
            return ("opt", STR)
        if name in {"len", "size_of"}:
            self._args_unchecked(args)
            return INT
        if name == "type_name":
            self._args_unchecked(args)
            return STR
        if name in {"max", "min"} and len(args) == 2:
            a = self.expr(args[0].value)
            b = self.expr(args[1].value, a)
            return _join(a, b) or UNKNOWN
        if name == "abs" and len(args) == 1:
            return self.expr(args[0].value)
        if name in {"Some", "Ok", "Err"}:
            if len(args) != 1:
                self.error("E111", f"`{name}` takes 1 argument", span)
                self._args_unchecked(args)
                return UNKNOWN
            if name == "Some":
                inner = expected[1] if isinstance(expected, tuple) and expected[0] == "opt" else None
                return ("opt", self.expr(args[0].value, inner))
            res = expected if isinstance(expected, tuple) and expected[0] == "res" else None
            if res is None and self.fn is not None and not self.fn.is_lambda:
                ret = self.fn.ret
                res = ret if isinstance(ret, tuple) and ret[0] == "res" else None
            if name == "Ok":
                t = self.expr(args[0].value, res[1] if res else None)
                return ("res", t, res[2] if res else UNKNOWN)
            t = self.expr(args[0].value, res[2] if res else None)
            return ("res", res[1] if res else UNKNOWN, t)
        return None

    def _method_call(self, field: ast.FieldExpr, args, expected, span):
        recv_node = field.recv
        mname = field.name
        recv = self.expr(recv_node)
        if isinstance(recv, tuple) and recv[0] == "type":
            return self._static_call(recv[1], mname, args, expected, span)
        if recv == UNKNOWN and isinstance(recv_node, ast.NameExpr) \
                and self.lookup(recv_node.name) is None:
            return self._library_static(recv_node.name, mname, args, expected, span)
        if isinstance(recv, tuple) and recv[0] in {"struct", "enum"}:
            type_name = recv[1]
            sig = self.methods.get(type_name, {}).get(mname)
            if sig is not None:
                if sig.has_self and sig.self_mut:
                    self._require_mutable(recv_node, span, mname)
                return self._call_sig(sig, args, span, f"{type_name}.{mname}")
            if recv[0] == "struct":
                ft = self._field_types(type_name).get(mname)
                if ft is not None:
                    return self._call_value(ft, args, mname, span)
            self.error("E104", f"`{type_name}` has no method `{mname}`", field.span)
            self._args_unchecked(args)
            return UNKNOWN
        if recv == UNKNOWN:
            self._args_unchecked(args)
            return UNKNOWN
        if mname in _MUTATING_METHODS and isinstance(recv, tuple) and recv[0] in {"vec", "map"}:
            self._require_mutable(recv_node, span, mname)
        result = self._builtin_method(recv, mname, args, expected, span)
        if result is None:
            self.error("E104", f"`{show(recv)}` has no method `{mname}`", field.span)
            self._args_unchecked(args)
            return UNKNOWN
        return result

    def _static_call(self, type_name: str, mname: str, args, expected, span):
        if type_name in self.enums and self._variant(type_name, mname) is not None:
            return self._variant_ctor(type_name, mname, args, span)
        sig = self.methods.get(type_name, {}).get(mname)
        if sig is not None:
            return self._call_sig(sig, args, span, f"{type_name}.{mname}")
        self.error("E104", f"`{type_name}` has no associated function `{mname}`", span)
        self._args_unchecked(args)
        return UNKNOWN

    def _library_static(self, module: str, mname: str, args, expected, span):
        """`Module.member(...)` for builtin modules and library types."""
        if module == "Vec" and mname == "new" and not args:
            return expected if isinstance(expected, tuple) and expected[0] == "vec" else ("vec", UNKNOWN)
        if module == "Map" and mname == "new" and not args:
            if isinstance(expected, tuple) and expected[0] == "map":
                return expected
            return ("map", UNKNOWN, UNKNOWN)
        if module == "Math":
            types = [self.expr(a.value, FLOAT) for a in args]
            if mname in {"floor", "ceil", "round"}:
                return INT
            if mname in {"abs", "max", "min"} and types:
                return FLOAT if FLOAT in types else (types[0] if types[0] != UNKNOWN else UNKNOWN)
            return FLOAT
        if module == "Process":
            if mname == "args":
                return ("vec", STR)
            if mname == "exit":
                for a in args:
                    self.expect(self.expr(a.value, INT), INT, a.value.span, "exit status")
                return NEVER
            if mname == "env":
                for a in args:
                    self.expect(self.expr(a.value, STR), STR, a.value.span, "variable name")
                return ("opt", STR)
            self.error("E104", f"`Process` has no function `{mname}`", span,
                       "available: `Process.args()`, `Process.env(name)`, `Process.exit(code)`")
            return UNKNOWN
        if module == "File":
            types = [self.expr(a.value, STR) for a in args]
            if mname == "exists":
                return BOOL
            if mname == "read_to_string":
                return ("res", STR, STR)
            if mname == "write":
                return ("res", BOOL, STR)
            return UNKNOWN
        self._args_unchecked(args)
        return UNKNOWN

    # -- builtin methods ------------------------------------------------------
    def _builtin_has_method(self, recv, name: str) -> bool:
        return name in _BUILTIN_METHOD_NAMES.get(_builtin_kind(recv), ())

    def _margs(self, args, expected_types, label: str, span, min_args=None):
        """Check method arguments against expected types; returns arg types."""
        lo = len(expected_types) if min_args is None else min_args
        if not lo <= len(args) <= len(expected_types):
            want = (f"{len(expected_types)}" if lo == len(expected_types)
                    else f"{lo} to {len(expected_types)}")
            self.error("E111", f"`{label}` takes {want} argument"
                       f"{'s' if want != '1' else ''} but {len(args)} "
                       f"{'were' if len(args) != 1 else 'was'} given", span)
            self._args_unchecked(args)
            return [UNKNOWN] * len(expected_types)
        out = []
        for a, et in zip(args, expected_types):
            t = self.expr(a.value, et)
            if et is not None:
                self.expect(t, et, a.value.span, f"argument of `{label}`")
            out.append(t)
        return out + [UNKNOWN] * (len(expected_types) - len(args))

    def _builtin_method(self, recv, m: str, args, expected, span):
        kind = _builtin_kind(recv)
        if kind is None:
            return UNKNOWN
        if m not in _BUILTIN_METHOD_NAMES[kind]:
            return None
        label = f"{show(recv)}.{m}"
        A = lambda *types, **kw: self._margs(args, list(types), label, span, **kw)  # noqa: E731
        if kind == "str":
            if m in {"len"}:
                A(); return INT
            if m == "is_empty":
                A(); return BOOL
            if m in {"trim", "trim_start", "trim_end", "to_upper", "to_uppercase",
                     "to_lower", "to_lowercase", "to_string"}:
                A(); return STR
            if m in {"contains", "starts_with", "ends_with"}:
                A(STR); return BOOL
            if m in {"strip_prefix", "strip_suffix"}:
                A(STR); return ("opt", STR)
            if m == "find":
                A(STR); return ("opt", INT)
            if m == "compare":
                A(STR); return INT
            if m == "replace":
                A(STR, STR); return STR
            if m == "repeat":
                A(INT); return STR
            if m == "split":
                A(STR, min_args=0); return ("vec", STR)
            if m == "split_once":
                A(STR); return ("opt", ("tup", STR, STR))
            if m in {"lines", "chars"}:
                A(); return ("vec", STR)
            if m == "bytes":
                A(); return ("vec", INT)
            if m == "slice":
                t = A(None)[0]
                return STR
            if m == "parse":
                if len(args) != 1:
                    A(None)
                    return UNKNOWN
                target = UNKNOWN
                node = args[0].value
                if isinstance(node, ast.NameExpr):
                    target = self.resolve(ast.TypeName(node.span, node.name, []))
                    if target == UNKNOWN or isinstance(target, tuple):
                        target = STR if node.name == "str" else UNKNOWN
                return ("res", target, STR)
        if kind == "int":
            if m == "to_string":
                A(); return STR
            if m == "to_float":
                A(); return FLOAT
            if m in {"abs", "signum"}:
                A(); return INT
            if m in {"is_even", "is_odd"}:
                A(); return BOOL
            if m in {"max", "min", "pow"}:
                A(INT); return INT
            if m == "clamp":
                A(INT, INT); return INT
        if kind == "float":
            if m == "to_string":
                A(); return STR
            if m in {"floor", "ceil", "round", "to_int"}:
                A(); return INT
            if m in {"sqrt", "abs"}:
                A(); return FLOAT
            if m == "is_nan":
                A(); return BOOL
            if m in {"max", "min", "pow"}:
                A(FLOAT); return FLOAT
        if kind == "bool":
            A(); return STR
        if kind == "range":
            A()
            return INT if m == "len" else ("vec", INT)
        if kind == "tup":
            A(); return INT
        if kind == "vec":
            return self._vec_method(recv, m, A, args, label, span)
        if kind == "map":
            k, v = recv[1], recv[2]
            if m in {"len"}:
                A(); return INT
            if m == "is_empty":
                A(); return BOOL
            if m in {"get", "get_mut"}:
                A(k); return ("opt", v)
            if m == "insert":
                A(k, v); return VOID
            if m == "remove":
                A(k); return ("opt", v)
            if m == "clear":
                A(); return VOID
            if m == "contains_key":
                A(k); return BOOL
            if m == "keys":
                A(); return ("vec", k)
            if m == "values":
                A(); return ("vec", v)
            if m in {"entries", "iter"}:
                A(); return ("vec", ("tup", k, v))
        if kind == "opt":
            t = recv[1]
            if m in {"is_some", "is_none"}:
                A(); return BOOL
            if m == "unwrap":
                A(); return t
            if m == "expect":
                A(STR); return t
            if m == "unwrap_or":
                A(t); return t
            if m == "unwrap_or_else":
                A(("fn", t)); return t
            if m == "map":
                f = A(("fn", UNKNOWN, t))[0]
                return ("opt", _fn_ret(f))
            if m == "and_then":
                f = A(("fn", UNKNOWN, t))[0]
                r = _fn_ret(f)
                return r if isinstance(r, tuple) and r[0] == "opt" else ("opt", UNKNOWN)
            if m == "filter":
                A(("fn", BOOL, t)); return recv
            if m == "or":
                A(recv); return recv
            if m == "ok_or":
                e = A(None)[0]
                return ("res", t, e)
        if kind == "res":
            t, e = recv[1], recv[2]
            if m in {"is_ok", "is_err"}:
                A(); return BOOL
            if m == "unwrap":
                A(); return t
            if m == "expect":
                A(STR); return t
            if m == "unwrap_err":
                A(); return e
            if m == "unwrap_or":
                A(t); return t
            if m == "unwrap_or_else":
                A(("fn", t, e)); return t
            if m == "ok":
                A(); return ("opt", t)
            if m == "err":
                A(); return ("opt", e)
            if m == "map":
                f = A(("fn", UNKNOWN, t))[0]
                return ("res", _fn_ret(f), e)
            if m == "map_err":
                f = A(("fn", UNKNOWN, e))[0]
                return ("res", t, _fn_ret(f))
            if m == "and_then":
                f = A(("fn", UNKNOWN, t))[0]
                r = _fn_ret(f)
                return r if isinstance(r, tuple) and r[0] == "res" else ("res", UNKNOWN, e)
        return UNKNOWN

    def _vec_method(self, recv, m, A, args, label, span):
        t = recv[1]
        if m == "len":
            A(); return INT
        if m == "is_empty":
            A(); return BOOL
        if m == "push":
            A(t); return VOID
        if m == "pop":
            A(); return ("opt", t)
        if m == "insert":
            A(INT, t); return VOID
        if m == "remove":
            A(INT); return t
        if m == "clear":
            A(); return VOID
        if m == "extend":
            A(recv); return VOID
        if m in {"first", "last", "min", "max"}:
            A(); return ("opt", t)
        if m in {"get", "get_mut"}:
            A(INT); return ("opt", t)
        if m == "contains":
            A(t); return BOOL
        if m == "index_of":
            A(t); return ("opt", INT)
        if m == "slice":
            A(None); return recv
        if m in {"take", "skip"}:
            A(INT); return recv
        if m in {"to_vec", "collect", "iter", "clone", "reverse", "sort", "dedup"}:
            A(); return recv
        if m == "sort_by":
            A(("fn", INT, t, t)); return recv
        if m == "map":
            f = A(("fn", UNKNOWN, t))[0]
            return ("vec", _fn_ret(f))
        if m == "filter":
            A(("fn", BOOL, t)); return recv
        if m == "fold":
            if len(args) != 2:
                A(None, None)
                return UNKNOWN
            init = self.expr(args[0].value)
            f = self.expr(args[1].value, ("fn", init, init, t))
            r = _fn_ret(f)
            if not compatible(r, init) and not compatible(init, r):
                self.error("E110", f"`fold` accumulator is `{show(init)}` but the "
                           f"closure returns `{show(r)}`", args[1].value.span)
            return _join(init, r) or init
        if m in {"any", "all"}:
            A(("fn", BOOL, t)); return BOOL
        if m == "count":
            A(("fn", BOOL, t)); return INT
        if m == "find":
            A(("fn", BOOL, t)); return ("opt", t)
        if m == "position":
            A(("fn", BOOL, t)); return ("opt", INT)
        if m == "for_each":
            A(("fn", UNKNOWN, t)); return VOID
        if m == "enumerate":
            A(); return ("vec", ("tup", INT, t))
        if m == "zip":
            other = A(None)[0]
            u = other[1] if isinstance(other, tuple) and other[0] == "vec" else UNKNOWN
            return ("vec", ("tup", t, u))
        if m in {"windows", "chunks"}:
            A(INT); return ("vec", recv)
        if m == "sum":
            A()
            if t not in (INT, FLOAT, UNKNOWN):
                self.error("E110", f"`sum` requires numeric elements, found `{show(t)}`", span)
            return t
        if m == "join":
            A(STR, min_args=0); return STR
        return UNKNOWN

    # -- closures -------------------------------------------------------------
    def _lambda(self, e: ast.LambdaExpr, expected):
        exp_params = list(expected[2:]) if (isinstance(expected, tuple) and expected[0] == "fn"
                                            and len(expected) - 2 == len(e.params)) else None
        params = []
        for i, p in enumerate(e.params):
            if p.type is not None:
                t = self.resolve(p.type)
            elif exp_params is not None:
                t = exp_params[i]
            else:
                t = UNKNOWN
            params.append((p.name, t))
        declared = self.resolve(e.ret) if e.ret is not None else None
        outer = self.fn
        lam = _FnCtx(outer.name if outer else "<closure>", declared or UNKNOWN,
                     outer.self_type if outer else None,
                     outer.self_mut if outer else False, is_lambda=True)
        self.fn = lam
        self.push()
        for name, t in params:
            self.bind(name, t)
        if isinstance(e.body, ast.Block):
            body_t = self.block(e.body, want_value=True)
        else:
            exp_ret = declared
            if exp_ret is None and isinstance(expected, tuple) and expected[0] == "fn":
                exp_ret = expected[1] if expected[1] != UNKNOWN else None
            body_t = self.expr(e.body, exp_ret)
        self.pop()
        self.fn = outer
        if declared is not None:
            self.expect(body_t, declared, e.span, "closure body")
            body_t = declared
        return ("fn", body_t, *[t for _, t in params])

    # -- error handling -------------------------------------------------------
    def _try(self, e: ast.TryExpr, expected):
        t = self.expr(e.expr)
        if not (isinstance(t, tuple) and t[0] in {"res", "opt"}):
            if e.or_else is not None:
                self.expr(e.or_else)
            if e.catch_expr is not None:
                self.push()
                self.bind(e.catch_name, UNKNOWN)
                self.expr(e.catch_expr)
                self.pop()
            return t
        ok_t, err_t = self._unwrap_fallible(t)
        if e.or_else is not None:
            fb = self.expr(e.or_else, ok_t)
            joined = _join(ok_t, fb)
            if joined is None:
                self.error("E110", f"`try ... or` fallback must be `{show(ok_t)}`, "
                           f"found `{show(fb)}`", e.or_else.span)
                return ok_t
            return joined
        if e.catch_expr is not None:
            self.push()
            self.bind(e.catch_name, err_t)
            fb = self.expr(e.catch_expr, ok_t)
            self.pop()
            joined = _join(ok_t, fb)
            if joined is None:
                self.error("E110", f"`catch` fallback must be `{show(ok_t)}`, "
                           f"found `{show(fb)}`", e.catch_expr.span)
                return ok_t
            return joined
        fn = self.fn
        if fn is not None and not fn.is_lambda and fn.ret not in (UNKNOWN,):
            ret = fn.ret
            if t[0] == "res":
                if not (isinstance(ret, tuple) and ret[0] == "res"):
                    self.error(
                        "E110",
                        f"`try` propagates an error, but `{fn.name}` returns `{show(ret)}`",
                        e.span,
                        "return a Result from this function, or handle the error with "
                        "`try ... catch e then ...` / `try ... or fallback`",
                    )
                elif not (compatible(err_t, ret[2]) or compatible(ret[2], err_t)):
                    self.error("E110", f"`try` propagates `{show(err_t)}` errors, but "
                               f"`{fn.name}` returns `{show(ret)}`", e.span)
            elif not (isinstance(ret, tuple) and ret[0] == "opt"):
                self.error(
                    "E110",
                    f"`try` on an Option propagates `none`, but `{fn.name}` returns "
                    f"`{show(ret)}`",
                    e.span,
                    "return an Option from this function, or use `try ... or fallback`",
                )
        return ok_t

    def _pipe(self, e: ast.PipeExpr):
        left = self.expr(e.left)
        right = e.right
        if isinstance(right, ast.CallExpr) and isinstance(right.callee, ast.NameExpr) \
                and right.callee.name in self.functions and self.lookup(right.callee.name) is None:
            sig = self.functions[right.callee.name]
            if sig.params:
                self.expect(left, sig.params[0][1], e.left.span,
                            f"piped argument of `{sig.name}`")
                rest = _Sig(sig.name, sig.params[1:], sig.ret)
                return self._call_sig(rest, right.args, right.span, sig.name)
        if isinstance(right, ast.CallExpr):
            for a in right.args:
                self.expr(a.value)
            return UNKNOWN
        t = self.expr(right)
        if isinstance(t, tuple) and t[0] == "fn":
            return t[1]
        return UNKNOWN

    # -- match ----------------------------------------------------------------
    def _match(self, e: ast.MatchExpr, expected, want_value: bool = True):
        scr = self.expr(e.scrutinee)
        result = None
        for arm in e.arms:
            binds: dict = {}
            self.pattern(arm.pattern, scr, binds)
            self.push()
            for name, (t, mutable) in binds.items():
                self.bind(name, t, mutable)
            if arm.guard is not None:
                self.expr(arm.guard)
            t = self.block(arm.body, want_value=want_value)
            self.pop()
            if not want_value:
                continue
            if t in (VOID,):
                result = VOID if result in (None, VOID, NEVER) else result
                continue
            if result is None or result == NEVER:
                result = t
            elif result != VOID:
                joined = _join(result, t)
                if joined is None:
                    last = arm.body.stmts[-1] if arm.body.stmts else arm
                    self.error("E110", f"`match` arms have incompatible types "
                               f"`{show(result)}` and `{show(t)}`", last.span)
                else:
                    result = joined
        missing = self._missing_patterns(scr, e.arms)
        e._exhaustive = not missing
        if missing:
            listed = ", ".join(f"`{m}`" for m in missing[:4])
            self.error("E112", f"non-exhaustive match: {listed} not covered", e.span,
                       "add the missing cases or a catch-all `case _ then ...` arm")
        return result if result is not None else VOID

    def pattern(self, pat, t, binds: dict) -> None:
        """Check `pat` against type `t`, collecting bindings into `binds`."""
        k = type(pat)
        if k is ast.WildcardPattern:
            return
        if k is ast.BindingPattern:
            binds[pat.name] = (t, False)
            return
        if k is ast.LiteralPattern:
            lt = self.expr(pat.value, t if t != UNKNOWN else None)
            if not (compatible(lt, t) or compatible(t, lt)):
                self.error("E110", f"pattern of type `{show(lt)}` cannot match a "
                           f"`{show(t)}` value", pat.span)
            return
        if k is ast.OrPattern:
            for alt in pat.alts:
                self.pattern(alt, t, binds)
            return
        if k is ast.TuplePattern:
            if isinstance(t, tuple) and t[0] == "tup":
                if len(t) - 1 != len(pat.elems):
                    self.error("E110", f"tuple pattern has {len(pat.elems)} elements but "
                               f"the value has {len(t) - 1}", pat.span)
                    for sub in pat.elems:
                        self.pattern(sub, UNKNOWN, binds)
                    return
                for sub, et in zip(pat.elems, t[1:]):
                    self.pattern(sub, et, binds)
                return
            if t != UNKNOWN:
                self.error("E110", f"tuple pattern cannot match a `{show(t)}` value", pat.span)
            for sub in pat.elems:
                self.pattern(sub, UNKNOWN, binds)
            return
        if k is ast.VariantPattern:
            self._variant_pattern(pat, t, binds)

    def _variant_pattern(self, pat: ast.VariantPattern, t, binds) -> None:
        name = pat.name
        subs = pat.subpatterns
        if name in {"Some", "None"}:
            if t != UNKNOWN and not (isinstance(t, tuple) and t[0] == "opt"):
                self.error("E110", f"`{name}` pattern cannot match a `{show(t)}` value", pat.span)
                t = ("opt", UNKNOWN)
            inner = t[1] if isinstance(t, tuple) else UNKNOWN
            for sub in subs:
                self.pattern(sub, inner, binds)
            return
        if name in {"Ok", "Err"}:
            if t != UNKNOWN and not (isinstance(t, tuple) and t[0] == "res"):
                self.error("E110", f"`{name}` pattern cannot match a `{show(t)}` value", pat.span)
                t = ("res", UNKNOWN, UNKNOWN)
            inner = (t[1] if name == "Ok" else t[2]) if isinstance(t, tuple) else UNKNOWN
            for sub in subs:
                self.pattern(sub, inner, binds)
            return
        enum_name = t[1] if isinstance(t, tuple) and t[0] == "enum" else self.variant_owner.get(name)
        if enum_name is None or enum_name not in self.enums:
            for sub in subs:
                self.pattern(sub, UNKNOWN, binds)
            return
        if isinstance(t, tuple) and t[0] not in {"enum"} and t != UNKNOWN:
            self.error("E110", f"variant pattern `{name}` cannot match a `{show(t)}` value",
                       pat.span)
        v = self._variant(enum_name, name)
        if v is None:
            self.error("E103", f"`{enum_name}` has no variant `{name}`", pat.span)
            for sub in subs:
                self.pattern(sub, UNKNOWN, binds)
            return
        types = self._payload_types(enum_name, v)
        names = [n for n, _ in v.payload]
        if len(subs) > len(types):
            self.error("E110", f"variant `{name}` has {len(types)} payload field"
                       f"{'s' if len(types) != 1 else ''} but the pattern has {len(subs)}",
                       pat.span)
        for i, sub in enumerate(subs):
            fname = pat.field_names[i] if i < len(pat.field_names) else None
            slot = names.index(fname) if fname in names else i
            if fname is not None and fname not in names:
                self.error("E103", f"variant `{name}` has no field `{fname}`", sub.span)
            self.pattern(sub, types[slot] if slot < len(types) else UNKNOWN, binds)

    # -- exhaustiveness -------------------------------------------------------
    def _missing_patterns(self, scr, arms) -> list[str]:
        rows = [[arm.pattern] for arm in arms if arm.guard is None]
        return self._uncovered(rows, [scr])

    def _irrefutable(self, p) -> bool:
        if isinstance(p, (ast.WildcardPattern, ast.BindingPattern)):
            return True
        if isinstance(p, ast.OrPattern):
            return any(self._irrefutable(a) for a in p.alts)
        if isinstance(p, ast.TuplePattern):
            return all(self._irrefutable(x) for x in p.elems)
        return False

    def _expand(self, rows):
        out = []
        for row in rows:
            if row and isinstance(row[0], ast.OrPattern):
                out.extend(self._expand([[alt, *row[1:]] for alt in row[0].alts]))
            else:
                out.append(row)
        return out

    def _infer_column_type(self, rows, t):
        if t != UNKNOWN:
            return t
        for row in rows:
            p = row[0]
            if isinstance(p, ast.VariantPattern):
                if p.name in {"Some", "None"}:
                    return ("opt", UNKNOWN)
                if p.name in {"Ok", "Err"}:
                    return ("res", UNKNOWN, UNKNOWN)
                if p.name in self.variant_owner:
                    return ("enum", self.variant_owner[p.name])
            if isinstance(p, ast.LiteralPattern) and isinstance(p.value, ast.BoolLit):
                return BOOL
            if isinstance(p, ast.TuplePattern):
                return ("tup", *[UNKNOWN] * len(p.elems))
            if isinstance(p, ast.LiteralPattern):
                return "literal"
        return UNKNOWN

    def _constructors(self, t):
        """[(name, payload types)] for types with a finite constructor set."""
        if t == BOOL:
            return [("true", []), ("false", [])]
        if isinstance(t, tuple):
            if t[0] == "opt":
                return [("Some", [t[1]]), ("None", [])]
            if t[0] == "res":
                return [("Ok", [t[1]]), ("Err", [t[2]])]
            if t[0] == "enum" and t[1] in self.enums:
                return [(v.name, self._payload_types(t[1], v)) for v in self.enums[t[1]].variants]
        return None

    def _specialize(self, p, ctor: str, arity: int):
        """Sub-patterns of `p` for constructor `ctor`, or None if `p` can't match it."""
        if self._irrefutable(p):
            return [ast.WildcardPattern(p.span)] * arity
        if isinstance(p, ast.LiteralPattern) and isinstance(p.value, ast.BoolLit):
            return [] if ("true" if p.value.value else "false") == ctor else None
        if isinstance(p, ast.VariantPattern) and p.name == ctor:
            subs = [ast.WildcardPattern(p.span)] * arity
            owner = self.variant_owner.get(ctor)
            names = []
            if owner is not None:
                v = self._variant(owner, ctor)
                names = [n for n, _ in v.payload] if v is not None else []
            for i, sub in enumerate(p.subpatterns):
                fname = p.field_names[i] if i < len(p.field_names) else None
                slot = names.index(fname) if fname in names else i
                if slot < arity:
                    subs[slot] = sub
            return subs
        return None

    def _uncovered(self, rows, types) -> list[str]:
        """Return descriptions of value shapes not covered by `rows`
        (each row is a list of patterns, one per entry in `types`)."""
        rows = self._expand(rows)
        if not types:
            return [] if rows else ["_"]
        if not rows:
            return ["_" if len(types) == 1 else "(" + ", ".join("_" for _ in types) + ")"]
        t = self._infer_column_type(rows, types[0])
        rest = types[1:]
        if isinstance(t, tuple) and t[0] == "tup":
            n = len(t) - 1
            spec = []
            for row in rows:
                p = row[0]
                if isinstance(p, ast.TuplePattern) and len(p.elems) == n:
                    spec.append([*p.elems, *row[1:]])
                elif self._irrefutable(p):
                    spec.append([*[ast.WildcardPattern(p.span)] * n, *row[1:]])
            if self._uncovered(spec, [*t[1:], *rest]):
                return ["(" + ", ".join("_" for _ in range(n)) + ")"]
            return []
        ctors = self._constructors(t)
        if ctors is None:
            # infinite domains (int, str, unknown): only catch-alls cover them
            if t == UNKNOWN and not any(not self._irrefutable(r[0]) for r in rows):
                return self._uncovered([r[1:] for r in rows], rest)
            if t == UNKNOWN:
                return []  # cannot reason about an unknown scrutinee
            wild = [r[1:] for r in rows if self._irrefutable(r[0])]
            if not wild:
                return ["_"]
            return self._uncovered(wild, rest)
        missing: list[str] = []
        for name, payload in ctors:
            spec = []
            for row in rows:
                subs = self._specialize(row[0], name, len(payload))
                if subs is not None:
                    spec.append([*subs, *row[1:]])
            sub_missing = self._uncovered(spec, [*payload, *rest])
            if sub_missing:
                if payload:
                    missing.append(f"{name}(" + ", ".join("_" for _ in payload) + ")")
                else:
                    missing.append(name)
        return missing


def _fn_ret(t):
    if isinstance(t, tuple) and t[0] == "fn":
        return t[1]
    return UNKNOWN


def _builtin_kind(t):
    if t in (INT, FLOAT, BOOL, STR):
        return t
    if isinstance(t, tuple) and t[0] in {"vec", "map", "opt", "res", "tup", "range"}:
        return t[0]
    return None


_BUILTIN_METHOD_NAMES = {
    "str": {
        "len", "is_empty", "trim", "trim_start", "trim_end", "to_upper",
        "to_uppercase", "to_lower", "to_lowercase", "to_string", "contains",
        "starts_with", "ends_with", "strip_prefix", "strip_suffix", "find",
        "compare", "replace", "repeat", "split", "split_once", "lines", "chars",
        "bytes", "slice", "parse",
    },
    "int": {"to_string", "to_float", "abs", "signum", "is_even", "is_odd", "max",
            "min", "pow", "clamp"},
    "float": {"to_string", "floor", "ceil", "round", "to_int", "sqrt", "abs",
              "is_nan", "max", "min", "pow"},
    "bool": {"to_string"},
    "range": {"len", "to_vec", "collect", "iter"},
    "tup": {"len"},
    "vec": {
        "len", "is_empty", "push", "pop", "insert", "remove", "clear", "extend",
        "first", "last", "min", "max", "get", "get_mut", "contains", "index_of",
        "slice", "take", "skip", "to_vec", "collect", "iter", "clone", "reverse",
        "sort", "dedup", "sort_by", "map", "filter", "fold", "any", "all",
        "count", "find", "position", "for_each", "enumerate", "zip", "windows",
        "chunks", "sum", "join",
    },
    "map": {"len", "is_empty", "get", "get_mut", "insert", "remove", "clear",
            "contains_key", "keys", "values", "entries", "iter"},
    "opt": {"is_some", "is_none", "unwrap", "expect", "unwrap_or", "unwrap_or_else",
            "map", "and_then", "filter", "or", "ok_or"},
    "res": {"is_ok", "is_err", "unwrap", "expect", "unwrap_err", "unwrap_or",
            "unwrap_or_else", "ok", "err", "map", "map_err", "and_then"},
}


def check_types(module: ast.Module) -> list[Diagnostic]:
    return TypeChecker(module).check()
