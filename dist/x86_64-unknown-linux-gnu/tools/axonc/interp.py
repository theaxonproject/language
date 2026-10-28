"""Tree-walking interpreter for the AXON AST.

Used by `axc run` and the REPL. The interpreter executes the full implemented
language surface; the C backend compiles the statically-typed core of the same
AST to native code.
"""

from __future__ import annotations

import math
import sys
import threading
from typing import Any

from . import ast
from .errors import Diagnostic
from . import stdlib
from .values import (
    AxEnum, AxExit, AxOption, AxPanic, AxRange, AxResult, AxStruct, AxType,
    NONE, ax_str, check_int, err, ok, some,
)


# Control-flow signals (Python exceptions used for non-local control flow).
class ReturnSignal(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class PropagateSignal(Exception):
    """Raised by `try`/`?` on failure; carries the value to return."""

    def __init__(self, value: Any) -> None:
        self.value = value


_SCALAR_TYPE_NAMES = {
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64",
    "float32", "float64", "bool", "byte", "rune", "str",
}


class Env:
    __slots__ = ("vars", "parent")

    def __init__(self, parent: "Env | None" = None) -> None:
        self.vars: dict[str, Any] = {}
        self.parent = parent

    def get(self, name: str) -> Any:
        env: Env | None = self
        while env is not None:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        raise KeyError(name)

    def has(self, name: str) -> bool:
        env: Env | None = self
        while env is not None:
            if name in env.vars:
                return True
            env = env.parent
        return False

    def define(self, name: str, value: Any) -> None:
        self.vars[name] = value

    def assign(self, name: str, value: Any) -> None:
        env: Env | None = self
        while env is not None:
            if name in env.vars:
                env.vars[name] = value
                return
            env = env.parent
        self.vars[name] = value


class AxClosure:
    def __init__(self, params: list[ast.Param], body: Any, env: Env) -> None:
        self.params = params
        self.body = body
        self.env = env


class Interpreter:
    def __init__(self, module: ast.Module, argv: list[str] | None = None) -> None:
        self.module = module
        self.argv = argv or []
        self.functions: dict[str, ast.FnDef] = {}
        self.methods: dict[str, dict[str, ast.FnDef]] = {}
        self.structs: dict[str, ast.StructDef] = {}
        self.enums: dict[str, ast.EnumDef] = {}
        self.enum_variant_owner: dict[str, str] = {}
        self.consts: dict[str, Any] = {}
        self.defer_stack: list[list[tuple[Any, Env]]] = []
        self._collect(module)

    def _collect(self, module: ast.Module) -> None:
        for item in module.items:
            if isinstance(item, ast.FnDef):
                self.functions[item.name] = item
            elif isinstance(item, ast.StructDef):
                if item.name != "__extern__":
                    self.structs[item.name] = item
            elif isinstance(item, ast.EnumDef):
                self.enums[item.name] = item
                for v in item.variants:
                    self.enum_variant_owner[v.name] = item.name
            elif isinstance(item, ast.ImplBlock):
                table = self.methods.setdefault(item.type_name, {})
                for m in item.methods:
                    table[m.name] = m
            elif isinstance(item, ast.TraitDef):
                # default trait method bodies are available for any impl
                table = self.methods.setdefault(item.name, {})
                for m in item.methods:
                    if m.body is not None:
                        table.setdefault(m.name, m)
        # trait default methods are inherited by every implementing type
        for item in module.items:
            if isinstance(item, ast.ImplBlock) and item.trait_name in self.methods:
                table = self.methods.setdefault(item.type_name, {})
                for mname, m in self.methods[item.trait_name].items():
                    table.setdefault(mname, m)
        for item in module.items:
            if isinstance(item, ast.Const):
                self.consts[item.name] = self.eval(item.value, Env())

    # -- entry points ---------------------------------------------------------
    def run_main(self) -> int:
        main = self.functions.get("main")
        if main is None:
            raise Diagnostic("E120", "program has no `main` function")
        try:
            result = self.call_function(main, [], {}, None)
        except AxExit as exc:
            return exc.code
        if result is None:
            return 0
        if isinstance(result, int) and not isinstance(result, bool):
            return result
        return 0

    # -- functions ------------------------------------------------------------
    def call_function(self, fn: ast.FnDef, args: list, kwargs: dict, self_val: Any) -> Any:
        env = Env(None)
        params = fn.params
        if params and params[0].is_self:
            env.define("self", self_val)
            params = params[1:]
        self._bind_params(fn, params, args, kwargs, env)
        try:
            if fn.body is not None:
                self.exec_block(fn.body, env)
            return None
        except ReturnSignal as r:
            return r.value
        except PropagateSignal as p:
            return p.value

    def _bind_params(self, fn, params, args, kwargs, env) -> None:
        i = 0
        for p in params:
            if p.is_variadic:
                env.define(p.name, list(args[i:]))
                i = len(args)
                continue
            if i < len(args):
                env.define(p.name, args[i]); i += 1
            elif p.name in kwargs:
                env.define(p.name, kwargs[p.name])
            elif p.default is not None:
                env.define(p.name, self.eval(p.default, env))
            else:
                env.define(p.name, None)

    def call_value(self, func: Any, args: list, kwargs: dict) -> Any:
        if isinstance(func, AxClosure):
            env = Env(func.env)
            self._bind_params(None, func.params, args, kwargs, env)
            try:
                if isinstance(func.body, ast.Block):
                    self.exec_block(func.body, env)
                    return None
                return self.eval(func.body, env)
            except ReturnSignal as r:
                return r.value
            except PropagateSignal as p:
                # `try` inside a closure returns the failure from the closure
                return p.value
        if isinstance(func, ast.FnDef):
            return self.call_function(func, args, kwargs, None)
        if isinstance(func, AxType):
            return AxType(func.name, tuple(args))
        if callable(func):
            try:
                return func(*args, **kwargs)
            except TypeError:
                return func(*args)
        raise AxPanic(f"value is not callable: {ax_str(func)}")

    def _run_defers(self) -> None:
        frame = self.defer_stack.pop()
        for expr, env in reversed(frame):
            self.eval(expr, env)

    # -- statements -----------------------------------------------------------
    def exec_block(self, block: ast.Block, env: Env) -> None:
        # `defer` is block-scoped: deferred expressions run, last-in
        # first-out, when the enclosing block exits by any route.
        self.defer_stack.append([])
        try:
            for stmt in block.stmts:
                self.exec_stmt(stmt, env)
        finally:
            self._run_defers()

    def exec_stmt(self, stmt: ast.Node, env: Env) -> None:
        kind = type(stmt)
        if kind is ast.LetStmt:
            self.exec_let(stmt, env)
        elif kind is ast.AssignStmt:
            self.exec_assign(stmt, env)
        elif kind is ast.ReturnStmt:
            raise ReturnSignal(self.eval(stmt.value, env) if stmt.value is not None else None)
        elif kind is ast.ExprStmt:
            self.eval(stmt.expr, env)
        elif kind is ast.IfStmt:
            self.exec_if(stmt, env)
        elif kind is ast.IfLetStmt:
            self.exec_if_let(stmt, env)
        elif kind is ast.ForStmt:
            self.exec_for(stmt, env)
        elif kind is ast.WhileStmt:
            self.exec_while(stmt, env)
        elif kind is ast.LoopStmt:
            self.exec_loop(stmt, env)
        elif kind is ast.BreakStmt:
            raise BreakSignal()
        elif kind is ast.ContinueStmt:
            raise ContinueSignal()
        elif kind is ast.DeferStmt:
            self.defer_stack[-1].append((stmt.expr, env))
        elif kind is ast.TaskStmt:
            self.eval(stmt.expr, env)
        elif kind is ast.TaskGroupStmt:
            self.exec_block(stmt.body, env)
        else:
            raise AxPanic(f"cannot execute statement {kind.__name__}")

    def exec_let(self, stmt: ast.LetStmt, env: Env) -> None:
        if stmt.catch_block is not None:
            inner = stmt.value.expr  # TryExpr.expr
            value = self.eval(inner, env)
            if isinstance(value, AxResult) and not value.is_ok:
                child = Env(env)
                child.define(stmt.catch_name, value.value)
                self.exec_block(stmt.catch_block, child)
                return
            if isinstance(value, AxOption) and not value.is_some:
                child = Env(env)
                child.define(stmt.catch_name, NONE)
                self.exec_block(stmt.catch_block, child)
                return
            unwrapped = value.value if isinstance(value, (AxResult, AxOption)) else value
            env.define(stmt.name, unwrapped)
            return
        value = self.eval(stmt.value, env) if stmt.value is not None else None
        if stmt.pattern is not None:
            if not self.match_pattern(stmt.pattern, value, env):
                raise AxPanic(f"`let` pattern did not match {ax_str(value)}")
            return
        env.define(stmt.name, value)

    def exec_assign(self, stmt: ast.AssignStmt, env: Env) -> None:
        value = self.eval(stmt.value, env)
        if stmt.op != "=":
            current = self.eval(stmt.target, env)
            value = self._apply_compound(stmt.op, current, value)
        target = stmt.target
        if isinstance(target, ast.NameExpr):
            env.assign(target.name, value)
        elif isinstance(target, ast.FieldExpr):
            obj = self.eval(target.recv, env)
            if isinstance(obj, AxStruct):
                obj.fields[target.name] = value
            elif isinstance(obj, dict):
                obj[target.name] = value
            else:
                setattr(obj, target.name, value)
        elif isinstance(target, ast.IndexExpr):
            obj = self.eval(target.recv, env)
            idx = self.eval(target.index, env)
            if isinstance(obj, list):
                obj[stdlib._vec_index(obj, int(idx))] = value
            else:
                obj[idx] = value
        else:
            raise AxPanic("invalid assignment target")

    def _apply_compound(self, op: str, a: Any, b: Any) -> Any:
        base = op[:-1]
        return self._binary(base, a, b)

    def exec_if(self, stmt: ast.IfStmt, env: Env) -> None:
        if self._truthy(self.eval(stmt.cond, env)):
            self.exec_block(stmt.then, Env(env)); return
        for cond, block in stmt.elifs:
            if self._truthy(self.eval(cond, env)):
                self.exec_block(block, Env(env)); return
        if stmt.els is not None:
            self.exec_block(stmt.els, Env(env))

    def exec_if_let(self, stmt: ast.IfLetStmt, env: Env) -> None:
        value = self.eval(stmt.expr, env)
        child = Env(env)
        if self.match_pattern(stmt.pattern, value, child):
            self.exec_block(stmt.then, child)
        elif stmt.els is not None:
            self.exec_block(stmt.els, Env(env))

    def exec_for(self, stmt: ast.ForStmt, env: Env) -> None:
        iterable = self.eval(stmt.iterable, env)
        for value in self._iterate(iterable):
            child = Env(env)
            self.match_pattern(stmt.pattern, value, child)
            try:
                self.exec_block(stmt.body, child)
            except BreakSignal:
                break
            except ContinueSignal:
                continue

    def exec_while(self, stmt: ast.WhileStmt, env: Env) -> None:
        while self._truthy(self.eval(stmt.cond, env)):
            try:
                self.exec_block(stmt.body, Env(env))
            except BreakSignal:
                break
            except ContinueSignal:
                continue

    def exec_loop(self, stmt: ast.LoopStmt, env: Env) -> None:
        while True:
            try:
                self.exec_block(stmt.body, Env(env))
            except BreakSignal:
                break
            except ContinueSignal:
                continue

    def _iterate(self, value: Any):
        if isinstance(value, AxRange):
            return iter(value)
        if isinstance(value, dict):
            return iter(value.items())
        return iter(value)

    # -- expressions ----------------------------------------------------------
    def eval(self, node: ast.Node, env: Env) -> Any:
        kind = type(node)
        if kind is ast.IntLit:
            return node.value
        if kind is ast.FloatLit:
            return node.value
        if kind is ast.BoolLit:
            return node.value
        if kind is ast.StrLit:
            return node.value
        if kind is ast.CharLit:
            return node.value
        if kind is ast.NoneLit:
            return NONE
        if kind is ast.NameExpr:
            return self.eval_name(node, env)
        if kind is ast.SelfExpr:
            return env.get("self")
        if kind is ast.FStr:
            return self.eval_fstr(node, env)
        if kind is ast.BinaryExpr:
            return self.eval_binary(node, env)
        if kind is ast.UnaryExpr:
            return self.eval_unary(node, env)
        if kind is ast.RefExpr:
            return self.eval(node.operand, env)
        if kind is ast.CallExpr:
            return self.eval_call(node, env)
        if kind is ast.FieldExpr:
            return self.eval_field(node, env)
        if kind is ast.OptFieldExpr:
            return self.eval_opt_field(node, env)
        if kind is ast.IndexExpr:
            return self.eval_index(node, env)
        if kind is ast.RangeExpr:
            return self.eval_range(node, env)
        if kind is ast.ListLit:
            return [self.eval(e, env) for e in node.elems]
        if kind is ast.ArrayRepeat:
            return [self.eval(node.value, env) for _ in range(int(self.eval(node.count, env)))]
        if kind is ast.TupleLit:
            return tuple(self.eval(e, env) for e in node.elems)
        if kind is ast.RecordLit:
            return self.eval_record(node, env)
        if kind is ast.IfExpr:
            return self.eval(node.then, env) if self._truthy(self.eval(node.cond, env)) else self.eval(node.els, env)
        if kind is ast.MatchExpr:
            return self.eval_match(node, env)
        if kind is ast.LambdaExpr:
            return AxClosure(node.params, node.body, env)
        if kind is ast.TryExpr:
            return self.eval_try(node, env)
        if kind is ast.PipeExpr:
            return self.eval_pipe(node, env)
        if kind is ast.CastExpr:
            return self.eval_cast(node, env)
        raise AxPanic(f"cannot evaluate {kind.__name__}")

    def eval_name(self, node: ast.NameExpr, env: Env) -> Any:
        name = node.name
        if env.has(name):
            return env.get(name)
        if name in self.consts:
            return self.consts[name]
        if name in self.functions:
            return self.functions[name]
        if name in stdlib.BUILTIN_FUNCTIONS:
            return stdlib.BUILTIN_FUNCTIONS[name]
        if name == "Some":
            return some
        if name == "Ok":
            return ok
        if name == "Err":
            return err
        if name in self.enum_variant_owner:
            owner = self.enum_variant_owner[name]
            return self._enum_constructor(owner, name)
        if name in self.structs or name in self.enums or name[0:1].isupper():
            return AxType(name)
        if name in _SCALAR_TYPE_NAMES:
            return AxType(name)
        raise AxPanic(f"unknown symbol: {name}")

    def _enum_constructor(self, owner: str, variant: str):
        vdef = next(v for v in self.enums[owner].variants if v.name == variant)
        if not vdef.payload:
            return AxEnum(owner, variant, [], [])
        names = [n for n, _ in vdef.payload]

        def construct(*vals, **kwargs):
            payload = list(vals)
            for name in names[len(payload):]:
                if name is not None and name in kwargs:
                    payload.append(kwargs[name])
            return AxEnum(owner, variant, payload, names)

        return construct

    def eval_fstr(self, node: ast.FStr, env: Env) -> str:
        out = []
        for part in node.parts:
            if isinstance(part, str):
                out.append(part)
            else:
                out.append(ax_str(self.eval(part, env)))
        return "".join(out)

    def eval_binary(self, node: ast.BinaryExpr, env: Env) -> Any:
        op = node.op
        if op == "and":
            return self.eval(node.right, env) if self._truthy(self.eval(node.left, env)) else False
        if op == "or":
            left = self.eval(node.left, env)
            return left if self._truthy(left) else self.eval(node.right, env)
        return self._binary(op, self.eval(node.left, env), self.eval(node.right, env))

    def _binary(self, op: str, a: Any, b: Any) -> Any:
        if op == "+":
            if isinstance(a, str) != isinstance(b, str):
                raise AxPanic("cannot add `str` and a non-str value")
            return check_int(a + b)
        if op == "-":
            return check_int(a - b)
        if op == "*":
            if isinstance(a, str):
                return a * max(0, b)
            return check_int(a * b)
        if op == "/":
            if isinstance(a, int) and isinstance(b, int):
                if b == 0:
                    raise AxPanic("integer division by zero")
                q = abs(a) // abs(b)
                return check_int(q if (a < 0) == (b < 0) else -q)
            if b == 0:
                # IEEE 754 semantics, like the native backend
                if a == 0 or a != a:
                    return math.nan
                return math.copysign(math.inf, a) * math.copysign(1.0, b)
            return a / b
        if op == "%":
            if isinstance(a, int) and isinstance(b, int):
                if b == 0:
                    raise AxPanic("modulo by zero")
                r = abs(a) % abs(b)  # C semantics: sign follows the dividend
                return r if a >= 0 else -r
            if b == 0:
                return float("nan")
            return math.fmod(a, b)
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "<<":
            if not 0 <= b < 64:
                raise AxPanic("shift amount out of range")
            v = (a << b) & 0xFFFFFFFFFFFFFFFF  # two's-complement wrap like C
            return v - (1 << 64) if v >= 1 << 63 else v
        if op == ">>":
            if not 0 <= b < 64:
                raise AxPanic("shift amount out of range")
            return a >> b
        if op == "&":
            return a & b
        if op == "|":
            return a | b
        if op == "^":
            return a ^ b
        if op == "is":
            return type(a).__name__ == ax_str(b)
        raise AxPanic(f"unsupported operator: {op}")

    def eval_unary(self, node: ast.UnaryExpr, env: Env) -> Any:
        v = self.eval(node.operand, env)
        op = node.op
        if op == "-":
            return check_int(-v)
        if op == "+":
            return +v
        if op == "not":
            return not self._truthy(v)
        if op == "~":
            return ~v
        if op == "*":
            return v  # dereference is transparent in the interpreter
        raise AxPanic(f"unsupported unary operator: {op}")

    def eval_call(self, node: ast.CallExpr, env: Env) -> Any:
        callee = node.callee
        if isinstance(callee, ast.FieldExpr):
            return self.eval_method_call(callee, node.args, env)
        if isinstance(callee, ast.OptFieldExpr):
            recv = self.eval(callee.recv, env)
            if isinstance(recv, AxOption) and not recv.is_some:
                return NONE
            if recv is None:
                return NONE
            base = recv.value if isinstance(recv, AxOption) else recv
            args, kwargs = self.eval_args(node.args, env)
            return some(self.dispatch_method(base, callee.name, args, kwargs))
        func = self.eval(callee, env)
        args, kwargs = self.eval_args(node.args, env)
        return self.call_value(func, args, kwargs)

    def eval_args(self, args: list[ast.Arg], env: Env) -> tuple[list, dict]:
        pos: list = []
        kw: dict = {}
        for arg in args:
            v = self.eval(arg.value, env)
            if arg.name is not None:
                kw[arg.name] = v
            else:
                pos.append(v)
        return pos, kw

    def eval_method_call(self, field: ast.FieldExpr, arg_nodes: list[ast.Arg], env: Env) -> Any:
        recv = self.eval(field.recv, env)
        args, kwargs = self.eval_args(arg_nodes, env)
        name = field.name
        if isinstance(recv, AxType):
            return self.dispatch_static(recv, name, args, kwargs)
        return self.dispatch_method(recv, name, args, kwargs)

    def dispatch_static(self, ty: AxType, name: str, args: list, kwargs: dict) -> Any:
        table = self.methods.get(ty.name)
        if table and name in table:
            return self.call_function(table[name], args, kwargs, None)
        if ty.name in self.enums:
            # enum variant constructor: Type.Variant(...)
            owner = ty.name
            if any(v.name == name for v in self.enums[owner].variants):
                cons = self._enum_constructor(owner, name)
                return cons(*args) if callable(cons) and not isinstance(cons, AxEnum) else cons
        return stdlib.builtin_static(self, ty, name, args, kwargs)

    def dispatch_method(self, recv: Any, name: str, args: list, kwargs: dict) -> Any:
        if isinstance(recv, AxStruct):
            table = self.methods.get(recv.type_name, {})
            if name in table:
                return self.call_function(table[name], args, kwargs, recv)
            if name in recv.fields and isinstance(recv.fields[name], (AxClosure, ast.FnDef)):
                return self.call_value(recv.fields[name], args, kwargs)
            raise AxPanic(f"`{recv.type_name}` has no method `{name}`")
        if isinstance(recv, AxEnum):
            table = self.methods.get(recv.type_name, {})
            if name in table:
                return self.call_function(table[name], args, kwargs, recv)
        return stdlib.builtin_method(self, recv, name, args, kwargs)

    def eval_field(self, node: ast.FieldExpr, env: Env) -> Any:
        recv = self.eval(node.recv, env)
        name = node.name
        if isinstance(recv, AxStruct):
            if name in recv.fields:
                return recv.fields[name]
            table = self.methods.get(recv.type_name, {})
            if name in table:
                fn = table[name]
                return lambda *a, **k: self.call_function(fn, list(a), k, recv)
            raise AxPanic(f"`{recv.type_name}` has no field `{name}`")
        if isinstance(recv, AxEnum):
            if name.isdigit():
                return recv.payload[int(name)]
            raise AxPanic(f"enum value has no field `{name}`")
        if isinstance(recv, tuple):
            return recv[int(name)]
        if isinstance(recv, AxType):
            table = self.methods.get(recv.name, {})
            if name in table:
                fn = table[name]
                return lambda *a, **k: self.call_function(fn, list(a), k, None)
            if recv.name in self.enums and any(v.name == name for v in self.enums[recv.name].variants):
                cons = self._enum_constructor(recv.name, name)
                return cons if isinstance(cons, AxEnum) else cons
            return stdlib.builtin_static_field(self, recv, name)
        return stdlib.builtin_object_field(recv, name)

    def eval_opt_field(self, node: ast.OptFieldExpr, env: Env) -> Any:
        recv = self.eval(node.recv, env)
        if isinstance(recv, AxOption):
            if not recv.is_some:
                return NONE
            recv = recv.value
        if recv is None:
            return NONE
        if isinstance(recv, AxStruct) and node.name in recv.fields:
            return some(recv.fields[node.name])
        return some(stdlib.builtin_object_field(recv, node.name))

    def eval_index(self, node: ast.IndexExpr, env: Env) -> Any:
        recv = self.eval(node.recv, env)
        index = self.eval(node.index, env)
        if isinstance(recv, dict):
            if index not in recv:
                raise AxPanic(f"missing map key: {ax_str(index)}")
            return recv[index]
        if isinstance(recv, (str, list)):
            return stdlib._apply_slice(recv, index)
        if isinstance(recv, tuple):
            return recv[int(index)]
        raise AxPanic(f"cannot index a value of type {type(recv).__name__}")

    def eval_range(self, node: ast.RangeExpr, env: Env) -> AxRange:
        start = int(self.eval(node.start, env)) if node.start is not None else 0
        end = int(self.eval(node.end, env)) if node.end is not None else start
        return AxRange(start, end, node.inclusive)

    def eval_record(self, node: ast.RecordLit, env: Env) -> Any:
        fields = {name: self.eval(value, env) for name, value in node.fields}
        if node.type_name in self.structs:
            return self.make_struct(node.type_name, fields)
        # unknown struct: build a generic record
        return AxStruct(node.type_name, fields)

    def make_struct(self, name: str, provided: dict) -> AxStruct:
        sdef = self.structs[name]
        fields: dict[str, Any] = {}
        for f in sdef.fields:
            if f.name in provided:
                fields[f.name] = provided[f.name]
            elif f.default is not None:
                fields[f.name] = self.eval(f.default, Env())
            else:
                fields[f.name] = self._default_for(f.type)
        # allow extra provided fields (defensive)
        for k, v in provided.items():
            if k not in fields:
                fields[k] = v
        return AxStruct(name, fields)

    def make_struct_from_dict(self, name: str, raw: dict) -> Any:
        if name not in self.structs:
            return raw
        return self.make_struct(name, {k: raw.get(k) for k in (f.name for f in self.structs[name].fields)})

    def _default_for(self, type_node: ast.Node) -> Any:
        if isinstance(type_node, ast.TypeName):
            n = type_node.name
            if n.startswith("int") or n.startswith("uint") or n in {"int", "uint", "byte"}:
                return 0
            if n.startswith("float"):
                return 0.0
            if n == "bool":
                return False
            if n == "str":
                return ""
        if isinstance(type_node, ast.OptionalType):
            return NONE
        return None

    def eval_try(self, node: ast.TryExpr, env: Env) -> Any:
        value = self.eval(node.expr, env)
        if isinstance(value, AxResult):
            if value.is_ok:
                return value.value
            return self._handle_try_failure(node, value.value, env, AxResult)
        if isinstance(value, AxOption):
            if value.is_some:
                return value.value
            return self._handle_try_failure(node, NONE, env, AxOption)
        return value

    def _handle_try_failure(self, node: ast.TryExpr, failval: Any, env: Env, kind) -> Any:
        if node.or_else is not None:
            return self.eval(node.or_else, env)
        if node.catch_expr is not None:
            child = Env(env)
            child.define(node.catch_name or "_", failval)
            return self.eval(node.catch_expr, child)
        # propagate the failure to the caller, keeping the Result/Option
        # wrapper so `try` at the call site still sees a failure value
        raise PropagateSignal(err(failval) if kind is AxResult else NONE)

    def eval_pipe(self, node: ast.PipeExpr, env: Env) -> Any:
        left = self.eval(node.left, env)
        right = node.right
        if isinstance(right, ast.CallExpr):
            args, kwargs = self.eval_args(right.args, env)
            if isinstance(right.callee, ast.FieldExpr):
                recv = self.eval(right.callee.recv, env)
                # x |> y.method(...) is unusual; treat as method on left
                return self.dispatch_method(left, right.callee.name, args, kwargs)
            func = self.eval(right.callee, env)
            return self.call_value(func, [left, *args], kwargs)
        func = self.eval(right, env)
        return self.call_value(func, [left], {})

    def eval_cast(self, node: ast.CastExpr, env: Env) -> Any:
        v = self.eval(node.expr, env)
        ty = node.type
        if isinstance(ty, ast.TypeName):
            n = ty.name
            if n.startswith("float"):
                return float(v)
            if n.startswith("int") or n.startswith("uint") or n in {"int", "uint", "byte"}:
                if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                    raise AxPanic("cannot cast a non-finite float to int")
                return check_int(int(v))
            if n == "str":
                return ax_str(v)
            if n == "bool":
                return self._truthy(v)
        return v

    def eval_match(self, node: ast.MatchExpr, env: Env) -> Any:
        value = self.eval(node.scrutinee, env)
        for arm in node.arms:
            child = Env(env)
            if self.match_pattern(arm.pattern, value, child):
                if arm.guard is not None and not self._truthy(self.eval(arm.guard, child)):
                    continue
                return self.run_block_value(arm.body, child)
        raise AxPanic(f"non-exhaustive match on {ax_str(value)}")

    def run_block_value(self, block: ast.Block, env: Env) -> Any:
        result = None
        self.defer_stack.append([])
        try:
            for i, stmt in enumerate(block.stmts):
                if i == len(block.stmts) - 1 and isinstance(stmt, ast.ExprStmt):
                    result = self.eval(stmt.expr, env)
                else:
                    self.exec_stmt(stmt, env)
        finally:
            self._run_defers()
        return result

    # -- pattern matching -----------------------------------------------------
    def match_pattern(self, pat: ast.Node, value: Any, env: Env) -> bool:
        kind = type(pat)
        if kind is ast.WildcardPattern:
            return True
        if kind is ast.BindingPattern:
            env.define(pat.name, value)
            return True
        if kind is ast.LiteralPattern:
            return self._values_eq(self.eval(pat.value, env), value)
        if kind is ast.OrPattern:
            return any(self.match_pattern(alt, value, env) for alt in pat.alts)
        if kind is ast.TuplePattern:
            if not isinstance(value, tuple) or len(value) != len(pat.elems):
                return False
            return all(self.match_pattern(p, v, env) for p, v in zip(pat.elems, value))
        if kind is ast.VariantPattern:
            return self._match_variant(pat, value, env)
        return False

    def _match_variant(self, pat: ast.VariantPattern, value: Any, env: Env) -> bool:
        name = pat.name
        if name == "Some":
            if isinstance(value, AxOption) and value.is_some:
                return not pat.subpatterns or self.match_pattern(pat.subpatterns[0], value.value, env)
            return False
        if name == "None":
            return isinstance(value, AxOption) and not value.is_some
        if name == "Ok":
            if isinstance(value, AxResult) and value.is_ok:
                return not pat.subpatterns or self.match_pattern(pat.subpatterns[0], value.value, env)
            return False
        if name == "Err":
            if isinstance(value, AxResult) and not value.is_ok:
                return not pat.subpatterns or self.match_pattern(pat.subpatterns[0], value.value, env)
            return False
        if isinstance(value, AxEnum) and value.variant == name:
            if not pat.subpatterns:
                return True
            for i, sub in enumerate(pat.subpatterns):
                target = value.payload[i] if i < len(value.payload) else None
                if not self.match_pattern(sub, target, env):
                    return False
            return True
        return False

    def _values_eq(self, a: Any, b: Any) -> bool:
        return a == b

    def _truthy(self, value: Any) -> bool:
        if isinstance(value, AxOption):
            return value.is_some
        return bool(value)


def run_with_deep_stack(fn, *args):
    """Run `fn(*args)` on a thread with a large stack.

    The tree-walking evaluator uses several Python frames per AXON call, so
    the default recursion limit would cap AXON recursion at a few hundred
    levels. A dedicated thread with a big stack lets deeply recursive AXON
    programs behave like their native counterparts; genuine runaway
    recursion is reported as a panic instead of crashing the host.
    """
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["value"] = fn(*args)
        except RecursionError:
            outcome["error"] = AxPanic("stack overflow")
        except BaseException as exc:  # re-raised on the calling thread
            outcome["error"] = exc

    old_limit = sys.getrecursionlimit()
    old_size = threading.stack_size()
    try:
        stack_mb = 0
        for mb in (512, 256, 64):  # some platforms cap thread stacks
            try:
                threading.stack_size(mb * 1024 * 1024)
                stack_mb = mb
                break
            except (ValueError, RuntimeError):
                continue
        # CPython 3.11+ runs Python-to-Python calls without consuming the C
        # stack; older versions need a limit the stack can actually hold.
        per_mb = 2000 if sys.version_info >= (3, 11) else 150
        limit = max(old_limit, min(1_000_000, stack_mb * per_mb))
        sys.setrecursionlimit(limit)
        worker = threading.Thread(target=target)
        worker.start()
        worker.join()
    finally:
        threading.stack_size(old_size)
        sys.setrecursionlimit(old_limit)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def interpret(module: ast.Module, argv: list[str] | None = None) -> int:
    return run_with_deep_stack(Interpreter(module, argv).run_main)
