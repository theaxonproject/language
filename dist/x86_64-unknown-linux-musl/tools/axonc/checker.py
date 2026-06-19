"""Semantic analysis for AXON.

This pass runs after parsing and before execution / code generation. It performs
name resolution (catching unknown value symbols and duplicate definitions) and
validates struct literals against their declarations. Diagnostics carry stable
error codes and precise spans.

It is intentionally conservative: identifiers that could refer to an external
type or imported module (PascalCase names, imported aliases) are accepted rather
than risk false positives, while undefined *value* identifiers — the common
class of real typo — are reported.
"""

from __future__ import annotations

from . import ast
from . import stdlib
from .errors import Diagnostic

# Builtin value functions and well-known library type/module names that are
# always in scope. The interpreter and C runtime provide these.
BUILTIN_VALUES = set(stdlib.BUILTIN_FUNCTIONS) | {
    "Some", "None", "Ok", "Err", "true", "false", "none", "self",
}

BUILTIN_TYPES = {
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64",
    "float32", "float64", "bool", "byte", "rune", "str", "void", "never", "any",
    "Option", "Result", "Vec", "Map", "Set", "Arc", "Rc", "Mutex", "RwLock",
    "Channel", "Math", "Json", "json", "File", "Io", "Http", "HttpClient",
    "HttpResponse", "HttpServer", "Net", "TcpListener", "TcpStream", "Cli",
    "App", "Process", "Task", "Core", "Instant", "Duration", "DateTime",
    "Logger", "LogLevel", "Request", "Response", "Router", "StatusCode",
    "env", "axstd", "str",
}


class Checker:
    def __init__(self, module: ast.Module) -> None:
        self.module = module
        self.diagnostics: list[Diagnostic] = []
        self.functions: dict[str, ast.FnDef] = {}
        self.structs: dict[str, ast.StructDef] = {}
        self.enums: dict[str, ast.EnumDef] = {}
        self.globals: set[str] = set(BUILTIN_VALUES) | set(BUILTIN_TYPES)
        self.enum_variants: set[str] = set()

    def error(self, code: str, message: str, span=None, help_text=None) -> None:
        self.diagnostics.append(Diagnostic(code, message, span, help_text))

    def check(self) -> list[Diagnostic]:
        self._collect()
        for item in self.module.items:
            if isinstance(item, ast.FnDef):
                self._check_fn(item, set())
            elif isinstance(item, ast.ImplBlock):
                self._check_impl(item)
            elif isinstance(item, ast.TraitDef):
                for m in item.methods:
                    if m.body is not None:
                        self._check_fn(m, {"self"})
        return self.diagnostics

    def _collect(self) -> None:
        seen_fn: dict[str, bool] = {}
        for use in self.module.uses:
            for n in use.names:
                self.globals.add(n)
            for seg in use.module:
                self.globals.add(seg)
        for item in self.module.items:
            if isinstance(item, ast.FnDef):
                if item.name in seen_fn:
                    self.error("E101", f"duplicate function `{item.name}`", item.span)
                seen_fn[item.name] = True
                self.functions[item.name] = item
                self.globals.add(item.name)
            elif isinstance(item, ast.StructDef):
                if item.name == "__extern__":
                    continue
                if item.name in self.structs:
                    self.error("E101", f"duplicate type `{item.name}`", item.span)
                self.structs[item.name] = item
                self.globals.add(item.name)
            elif isinstance(item, ast.EnumDef):
                self.enums[item.name] = item
                self.globals.add(item.name)
                for v in item.variants:
                    self.enum_variants.add(v.name)
                    self.globals.add(v.name)
            elif isinstance(item, ast.Const):
                self.globals.add(item.name)
            elif isinstance(item, ast.TraitDef):
                self.globals.add(item.name)

    def _check_impl(self, impl: ast.ImplBlock) -> None:
        if (impl.type_name not in self.structs and impl.type_name not in self.enums
                and impl.type_name not in BUILTIN_TYPES):
            self.error(
                "E102", f"impl for unknown type `{impl.type_name}`", impl.span,
                "declare the struct or enum before implementing methods on it",
            )
        for m in impl.methods:
            base = {"self"} if (m.params and m.params[0].is_self) else set()
            self._check_fn(m, base)

    def _check_fn(self, fn: ast.FnDef, extra_locals: set[str]) -> None:
        scope = set(extra_locals)
        for p in fn.params:
            if not p.is_self:
                scope.add(p.name)
        if fn.body is not None:
            self._check_block(fn.body, [scope])

    # -- statement walking ----------------------------------------------------
    def _check_block(self, block: ast.Block, scopes: list[set]) -> None:
        scopes = scopes + [set()]
        for stmt in block.stmts:
            self._check_stmt(stmt, scopes)

    def _check_stmt(self, stmt: ast.Node, scopes: list[set]) -> None:
        k = type(stmt)
        if k is ast.LetStmt:
            if stmt.value is not None:
                self._check_expr(stmt.value, scopes)
            scopes[-1].add(stmt.name)
            if stmt.catch_block is not None:
                inner = scopes + [{stmt.catch_name}]
                self._check_block(stmt.catch_block, inner)
        elif k is ast.AssignStmt:
            self._check_expr(stmt.value, scopes)
            self._check_expr(stmt.target, scopes)
        elif k is ast.ReturnStmt:
            if stmt.value is not None:
                self._check_expr(stmt.value, scopes)
        elif k is ast.ExprStmt:
            self._check_expr(stmt.expr, scopes)
        elif k is ast.DeferStmt:
            self._check_expr(stmt.expr, scopes)
        elif k is ast.IfStmt:
            self._check_expr(stmt.cond, scopes)
            self._check_block(stmt.then, scopes)
            for cond, block in stmt.elifs:
                self._check_expr(cond, scopes)
                self._check_block(block, scopes)
            if stmt.els is not None:
                self._check_block(stmt.els, scopes)
        elif k is ast.IfLetStmt:
            self._check_expr(stmt.expr, scopes)
            inner = scopes + [set(self._pattern_names(stmt.pattern))]
            self._check_block(stmt.then, inner)
            if stmt.els is not None:
                self._check_block(stmt.els, scopes)
        elif k is ast.ForStmt:
            self._check_expr(stmt.iterable, scopes)
            inner = scopes + [set(self._pattern_names(stmt.pattern))]
            self._check_block(stmt.body, inner)
        elif k is ast.WhileStmt:
            self._check_expr(stmt.cond, scopes)
            self._check_block(stmt.body, scopes)
        elif k is ast.LoopStmt:
            self._check_block(stmt.body, scopes)
        elif k is ast.TaskStmt:
            self._check_expr(stmt.expr, scopes)
        elif k is ast.TaskGroupStmt:
            self._check_block(stmt.body, scopes)
        # break/continue: nothing to resolve

    # -- expression walking ---------------------------------------------------
    def _check_expr(self, node: ast.Node, scopes: list[set]) -> None:
        k = type(node)
        if k is ast.NameExpr:
            self._check_name(node, scopes)
        elif k is ast.FStr:
            for part in node.parts:
                if not isinstance(part, str):
                    self._check_expr(part, scopes)
        elif k is ast.BinaryExpr:
            self._check_expr(node.left, scopes); self._check_expr(node.right, scopes)
        elif k is ast.UnaryExpr:
            self._check_expr(node.operand, scopes)
        elif k is ast.RefExpr:
            self._check_expr(node.operand, scopes)
        elif k is ast.CallExpr:
            self._check_expr(node.callee, scopes)
            for a in node.args:
                self._check_expr(a.value, scopes)
        elif k is ast.FieldExpr or k is ast.OptFieldExpr:
            self._check_expr(node.recv, scopes)
        elif k is ast.IndexExpr:
            self._check_expr(node.recv, scopes); self._check_expr(node.index, scopes)
        elif k is ast.RangeExpr:
            if node.start is not None:
                self._check_expr(node.start, scopes)
            if node.end is not None:
                self._check_expr(node.end, scopes)
        elif k is ast.ListLit:
            for e in node.elems:
                self._check_expr(e, scopes)
        elif k is ast.ArrayRepeat:
            self._check_expr(node.value, scopes); self._check_expr(node.count, scopes)
        elif k is ast.TupleLit:
            for e in node.elems:
                self._check_expr(e, scopes)
        elif k is ast.RecordLit:
            self._check_record(node, scopes)
        elif k is ast.IfExpr:
            self._check_expr(node.cond, scopes)
            self._check_expr(node.then, scopes)
            self._check_expr(node.els, scopes)
        elif k is ast.MatchExpr:
            self._check_expr(node.scrutinee, scopes)
            for arm in node.arms:
                inner = scopes + [set(self._pattern_names(arm.pattern))]
                if arm.guard is not None:
                    self._check_expr(arm.guard, inner)
                self._check_block(arm.body, inner)
        elif k is ast.LambdaExpr:
            inner = scopes + [{p.name for p in node.params}]
            if isinstance(node.body, ast.Block):
                self._check_block(node.body, inner)
            else:
                self._check_expr(node.body, inner)
        elif k is ast.TryExpr:
            self._check_expr(node.expr, scopes)
            if node.or_else is not None:
                self._check_expr(node.or_else, scopes)
            if node.catch_expr is not None:
                self._check_expr(node.catch_expr, scopes + [{node.catch_name}])
        elif k is ast.PipeExpr:
            self._check_expr(node.left, scopes); self._check_expr(node.right, scopes)
        elif k is ast.CastExpr:
            self._check_expr(node.expr, scopes)

    def _check_name(self, node: ast.NameExpr, scopes: list[set]) -> None:
        name = node.name
        for scope in scopes:
            if name in scope:
                return
        if name in self.globals:
            return
        if name and name[0].isupper():
            return  # potential external type / enum variant; accept
        self.error(
            "E100", f"unknown symbol `{name}`", node.span,
            "this name is not defined in the current scope",
        )

    def _check_record(self, node: ast.RecordLit, scopes: list[set]) -> None:
        for _, value in node.fields:
            self._check_expr(value, scopes)
        sdef = self.structs.get(node.type_name)
        if sdef is None:
            return  # external / generic record
        declared = {f.name for f in sdef.fields}
        for fname, _ in node.fields:
            if fname not in declared:
                self.error(
                    "E103",
                    f"`{node.type_name}` has no field `{fname}`",
                    node.span,
                )

    def _pattern_names(self, pat: ast.Node) -> list[str]:
        k = type(pat)
        if k is ast.BindingPattern:
            return [pat.name]
        if k is ast.VariantPattern:
            out: list[str] = []
            for sub in pat.subpatterns:
                out.extend(self._pattern_names(sub))
            return out
        if k is ast.TuplePattern:
            out = []
            for e in pat.elems:
                out.extend(self._pattern_names(e))
            return out
        if k is ast.OrPattern:
            out = []
            for alt in pat.alts:
                out.extend(self._pattern_names(alt))
            return out
        return []


def check_module(module: ast.Module) -> list[Diagnostic]:
    return Checker(module).check()
