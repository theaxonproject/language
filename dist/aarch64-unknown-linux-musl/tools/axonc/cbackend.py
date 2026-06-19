"""AXON native backend: AST -> C11.

Lowers the statically-typed core of the AXON language to portable C11. The
emitted C is compiled by the host C compiler (`cc`) into a genuine native
executable with no interpreter dependency.

The backend supports the compiled core: functions and recursion, the scalar
types (`int`/`uint` family, `float`, `bool`, `str`), structs with methods and
trait methods (static dispatch), control flow (`if`/`for` over ranges/`while`/
`loop`), arithmetic and comparison operators, f-strings, and the core I/O and
panic intrinsics. Constructs outside this core (closures, generics,
`match`, channels/tasks, the networked/JSON standard library) raise a clear
diagnostic directing the user to `axc run`, which executes the full language.
"""

from __future__ import annotations

import pathlib

from . import ast
from .errors import Diagnostic

_INT_TYPES = {
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64", "byte", "rune",
}
_FLOAT_TYPES = {"float32", "float64"}

RUNTIME_C = (pathlib.Path(__file__).parent / "runtime.c").read_text(encoding="utf-8")


def unsupported(message: str, span=None) -> Diagnostic:
    return Diagnostic(
        "E800", message, span,
        "this construct is not yet supported by the native backend; "
        "run the program with `axc run` instead",
    )


class Writer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.level = 0

    def line(self, text: str = "") -> None:
        self.parts.append(("    " * self.level + text) if text else "")

    def indent(self) -> None:
        self.level += 1

    def dedent(self) -> None:
        self.level -= 1

    def text(self) -> str:
        return "\n".join(self.parts) + "\n"


class FnContext:
    def __init__(self, self_type: str | None = None, self_ptr: bool = False) -> None:
        self.var_types: dict[str, str] = {}
        self.self_type = self_type
        self.self_ptr = self_ptr
        self._tmp = 0


class CBackend:
    def __init__(self, module: ast.Module) -> None:
        self.module = module
        self.structs: dict[str, ast.StructDef] = {}
        self.struct_fields: dict[str, dict[str, str]] = {}
        self.functions: dict[str, ast.FnDef] = {}
        self.methods: dict[str, dict[str, ast.FnDef]] = {}
        self._collect()

    def _collect(self) -> None:
        for item in self.module.items:
            if isinstance(item, ast.StructDef) and item.name != "__extern__":
                self.structs[item.name] = item
            elif isinstance(item, ast.FnDef):
                self.functions[item.name] = item
            elif isinstance(item, ast.ImplBlock):
                table = self.methods.setdefault(item.type_name, {})
                for m in item.methods:
                    table[m.name] = m
        for name, sdef in self.structs.items():
            self.struct_fields[name] = {
                f.name: self.type_tag(f.type) for f in sdef.fields
            }

    # -- type machinery -------------------------------------------------------
    def type_tag(self, node: ast.Node | None) -> str:
        if node is None:
            return "void"
        if isinstance(node, ast.TypeName):
            n = node.name
            if n in _INT_TYPES:
                return "int"
            if n in _FLOAT_TYPES:
                return "float"
            if n in {"bool", "str", "void"}:
                return n
            if n in self.structs:
                return n
            raise unsupported(f"native backend does not support type `{n}`", node.span)
        if isinstance(node, ast.RefType):
            return self.type_tag(node.inner)
        raise unsupported("native backend does not support this type form", node.span)

    def ctype(self, tag: str) -> str:
        return {
            "int": "int64_t",
            "float": "double",
            "bool": "bool",
            "str": "AxStr",
            "void": "void",
        }.get(tag, tag)

    # -- top-level generation -------------------------------------------------
    def generate(self) -> str:
        w = Writer()
        w.parts.append(RUNTIME_C)
        w.line()
        w.line("/* ---- program ---- */")
        for name in self.structs:
            self._emit_struct(w, self.structs[name])
        w.line()
        # prototypes
        for fn in self.functions.values():
            w.line(self._prototype(fn) + ";")
        for owner, table in self.methods.items():
            for m in table.values():
                w.line(self._prototype(m, owner) + ";")
        w.line()
        # bodies
        for fn in self.functions.values():
            self._emit_fn(w, fn)
        for owner, table in self.methods.items():
            for m in table.values():
                self._emit_fn(w, m, owner)
        self._emit_c_main(w)
        return w.text()

    def _emit_struct(self, w: Writer, sdef: ast.StructDef) -> None:
        if sdef.generics:
            raise unsupported(
                f"native backend does not support generic struct `{sdef.name}`",
                sdef.span,
            )
        w.line(f"typedef struct {sdef.name} {{")
        w.indent()
        for f in sdef.fields:
            w.line(f"{self.ctype(self.type_tag(f.type))} {f.name};")
        w.dedent()
        w.line(f"}} {sdef.name};")

    def _cname(self, fn: ast.FnDef, owner: str | None) -> str:
        if owner is not None:
            return f"{owner}__{fn.name}"
        if fn.name == "main":
            return "axon_user_main"
        return fn.name

    def _prototype(self, fn: ast.FnDef, owner: str | None = None) -> str:
        ret = self.ctype(self.type_tag(fn.return_type))
        params: list[str] = []
        rest = fn.params
        if rest and rest[0].is_self:
            self_decl = f"{owner}* self" if rest[0].is_mut else f"{owner} self"
            params.append(self_decl)
            rest = rest[1:]
        for p in rest:
            if p.type is None:
                raise unsupported(
                    f"parameter `{p.name}` needs a type annotation for native compilation",
                    p.span,
                )
            params.append(f"{self.ctype(self.type_tag(p.type))} {p.name}")
        param_str = ", ".join(params) if params else "void"
        return f"{ret} {self._cname(fn, owner)}({param_str})"

    def _emit_fn(self, w: Writer, fn: ast.FnDef, owner: str | None = None) -> None:
        if fn.generics:
            raise unsupported(f"native backend does not support generic fn `{fn.name}`", fn.span)
        if fn.body is None:
            return
        ctx = FnContext()
        rest = fn.params
        if rest and rest[0].is_self:
            ctx.self_type = owner
            ctx.self_ptr = rest[0].is_mut
            rest = rest[1:]
        for p in rest:
            ctx.var_types[p.name] = self.type_tag(p.type)
        w.line(self._prototype(fn, owner) + " {")
        w.indent()
        self._emit_block(w, fn.body, ctx)
        w.dedent()
        w.line("}")
        w.line()

    def _emit_c_main(self, w: Writer) -> None:
        main = self.functions.get("main")
        if main is None:
            return
        ret_tag = self.type_tag(main.return_type)
        w.line("int main(int argc, char **argv) {")
        w.indent()
        w.line("(void)argc; (void)argv;")
        if ret_tag == "int":
            w.line("return (int)axon_user_main();")
        else:
            w.line("axon_user_main();")
            w.line("return 0;")
        w.dedent()
        w.line("}")

    # -- statements -----------------------------------------------------------
    def _emit_block(self, w: Writer, block: ast.Block, ctx: FnContext) -> None:
        for stmt in block.stmts:
            self._emit_stmt(w, stmt, ctx)

    def _emit_stmt(self, w: Writer, stmt: ast.Node, ctx: FnContext) -> None:
        k = type(stmt)
        if k is ast.LetStmt:
            self._emit_let(w, stmt, ctx)
        elif k is ast.AssignStmt:
            self._emit_assign(w, stmt, ctx)
        elif k is ast.ReturnStmt:
            if stmt.value is None:
                w.line("return;")
            else:
                code, _ = self._emit_expr(stmt.value, ctx)
                w.line(f"return {code};")
        elif k is ast.ExprStmt:
            self._emit_expr_stmt(w, stmt.expr, ctx)
        elif k is ast.IfStmt:
            self._emit_if(w, stmt, ctx)
        elif k is ast.ForStmt:
            self._emit_for(w, stmt, ctx)
        elif k is ast.WhileStmt:
            cond, _ = self._emit_expr(stmt.cond, ctx)
            w.line(f"while ({cond}) {{")
            w.indent(); self._emit_block(w, stmt.body, ctx); w.dedent()
            w.line("}")
        elif k is ast.LoopStmt:
            w.line("while (1) {")
            w.indent(); self._emit_block(w, stmt.body, ctx); w.dedent()
            w.line("}")
        elif k is ast.BreakStmt:
            w.line("break;")
        elif k is ast.ContinueStmt:
            w.line("continue;")
        else:
            raise unsupported(
                f"native backend does not support `{k.__name__}` yet",
                getattr(stmt, "span", None),
            )

    def _emit_let(self, w: Writer, stmt: ast.LetStmt, ctx: FnContext) -> None:
        if stmt.catch_block is not None:
            raise unsupported("native backend does not support try/catch let", stmt.span)
        if stmt.value is None:
            tag = self.type_tag(stmt.type) if stmt.type is not None else "int"
            ctx.var_types[stmt.name] = tag
            w.line(f"{self.ctype(tag)} {stmt.name};")
            return
        code, tag = self._emit_expr(stmt.value, ctx)
        if stmt.type is not None:
            tag = self.type_tag(stmt.type)
        if tag == "unknown" or tag == "void":
            raise unsupported(
                f"cannot infer a native type for `{stmt.name}`", stmt.span,
            )
        ctx.var_types[stmt.name] = tag
        w.line(f"{self.ctype(tag)} {stmt.name} = {code};")

    def _emit_assign(self, w: Writer, stmt: ast.AssignStmt, ctx: FnContext) -> None:
        target, _ = self._emit_expr(stmt.target, ctx)
        value, vtag = self._emit_expr(stmt.value, ctx)
        if stmt.op == "=":
            w.line(f"{target} = {value};")
            return
        op = stmt.op[:-1]
        if op == "/" and vtag == "int":
            w.line(f"{target} = ax_idiv({target}, {value});")
        elif op == "%" and vtag == "int":
            w.line(f"{target} = ax_imod({target}, {value});")
        else:
            w.line(f"{target} = {target} {op} {value};")

    def _emit_if(self, w: Writer, stmt: ast.IfStmt, ctx: FnContext) -> None:
        cond, _ = self._emit_expr(stmt.cond, ctx)
        w.line(f"if ({cond}) {{")
        w.indent(); self._emit_block(w, stmt.then, ctx); w.dedent()
        for econd, eblock in stmt.elifs:
            ec, _ = self._emit_expr(econd, ctx)
            w.line(f"}} else if ({ec}) {{")
            w.indent(); self._emit_block(w, eblock, ctx); w.dedent()
        if stmt.els is not None:
            w.line("} else {")
            w.indent(); self._emit_block(w, stmt.els, ctx); w.dedent()
        w.line("}")

    def _emit_for(self, w: Writer, stmt: ast.ForStmt, ctx: FnContext) -> None:
        if not isinstance(stmt.iterable, ast.RangeExpr):
            raise unsupported(
                "native backend only supports `for x in a..b` range loops",
                stmt.span,
            )
        rng = stmt.iterable
        if isinstance(stmt.pattern, ast.BindingPattern):
            var = stmt.pattern.name
        else:
            var = "_axi"
        start, _ = self._emit_expr(rng.start, ctx) if rng.start is not None else ("0", "int")
        end, _ = self._emit_expr(rng.end, ctx) if rng.end is not None else (start, "int")
        cmp = "<=" if rng.inclusive else "<"
        ctx.var_types[var] = "int"
        w.line(f"for (int64_t {var} = {start}; {var} {cmp} {end}; {var}++) {{")
        w.indent(); self._emit_block(w, stmt.body, ctx); w.dedent()
        w.line("}")

    def _emit_expr_stmt(self, w: Writer, expr: ast.Node, ctx: FnContext) -> None:
        if isinstance(expr, ast.CallExpr) and isinstance(expr.callee, ast.NameExpr):
            name = expr.callee.name
            if name in {"println", "print", "eprintln", "eprint"}:
                self._emit_io(w, name, expr.args, ctx)
                return
            if name == "panic":
                msg = self._to_str(*self._emit_expr(expr.args[0].value, ctx)) if expr.args else "ax_str_lit(\"explicit panic\")"
                w.line(f"ax_panic({msg});")
                return
            if name in {"assert"}:
                cond, _ = self._emit_expr(expr.args[0].value, ctx)
                msg = (self._to_str(*self._emit_expr(expr.args[1].value, ctx))
                       if len(expr.args) > 1 else "ax_str_lit(\"assertion failed\")")
                w.line(f"ax_assert({cond}, {msg});")
                return
        code, _ = self._emit_expr(expr, ctx)
        w.line(f"{code};")

    def _emit_io(self, w: Writer, name: str, args: list[ast.Arg], ctx: FnContext) -> None:
        fn = {"println": "ax_println", "print": "ax_print",
              "eprintln": "ax_eprintln", "eprint": "ax_eprint"}[name]
        if not args:
            w.line(f'{fn}(ax_str_lit(""));')
            return
        pieces = [self._to_str(*self._emit_expr(a.value, ctx)) for a in args]
        acc = pieces[0]
        for p in pieces[1:]:
            acc = f'ax_str_concat(ax_str_concat({acc}, ax_str_lit(" ")), {p})'
        w.line(f"{fn}({acc});")

    # -- expressions ----------------------------------------------------------
    def _emit_expr(self, node: ast.Node, ctx: FnContext) -> tuple[str, str]:
        k = type(node)
        if k is ast.IntLit:
            return str(node.value), "int"
        if k is ast.FloatLit:
            text = repr(node.value)
            if "." not in text and "e" not in text:
                text += ".0"
            return text, "float"
        if k is ast.BoolLit:
            return ("true" if node.value else "false"), "bool"
        if k is ast.StrLit:
            return f"ax_str_lit({self._c_string(node.value)})", "str"
        if k is ast.CharLit:
            return f"ax_str_lit({self._c_string(node.value)})", "str"
        if k is ast.FStr:
            return self._emit_fstr(node, ctx), "str"
        if k is ast.NameExpr:
            return self._emit_name(node, ctx)
        if k is ast.SelfExpr:
            return ("(*self)" if ctx.self_ptr else "self"), (ctx.self_type or "unknown")
        if k is ast.BinaryExpr:
            return self._emit_binary(node, ctx)
        if k is ast.UnaryExpr:
            return self._emit_unary(node, ctx)
        if k is ast.RefExpr:
            return self._emit_expr(node.operand, ctx)
        if k is ast.CallExpr:
            return self._emit_call(node, ctx)
        if k is ast.FieldExpr:
            return self._emit_field(node, ctx)
        if k is ast.RecordLit:
            return self._emit_record(node, ctx)
        if k is ast.IfExpr:
            cond, _ = self._emit_expr(node.cond, ctx)
            then, t = self._emit_expr(node.then, ctx)
            els, _ = self._emit_expr(node.els, ctx)
            return f"({cond} ? {then} : {els})", t
        if k is ast.CastExpr:
            code, _ = self._emit_expr(node.expr, ctx)
            tag = self.type_tag(node.type)
            return f"(({self.ctype(tag)}){code})", tag
        raise unsupported(
            f"native backend does not support expression `{k.__name__}`",
            getattr(node, "span", None),
        )

    def _emit_name(self, node: ast.NameExpr, ctx: FnContext) -> tuple[str, str]:
        name = node.name
        if name in ctx.var_types:
            return name, ctx.var_types[name]
        raise unsupported(
            f"native backend cannot resolve `{name}` (only locals/params are supported)",
            node.span,
        )

    def _emit_binary(self, node: ast.BinaryExpr, ctx: FnContext) -> tuple[str, str]:
        op = node.op
        lcode, ltag = self._emit_expr(node.left, ctx)
        rcode, rtag = self._emit_expr(node.right, ctx)
        if op == "and":
            return f"({lcode} && {rcode})", "bool"
        if op == "or":
            return f"({lcode} || {rcode})", "bool"
        if op in {"==", "!="} and (ltag == "str" or rtag == "str"):
            base = f"ax_str_eq({lcode}, {rcode})"
            return (base if op == "==" else f"(!{base})"), "bool"
        if op == "+" and ltag == "str":
            return f"ax_str_concat({lcode}, {rcode})", "str"
        if op in {"==", "!=", "<", "<=", ">", ">="}:
            return f"({lcode} {op} {rcode})", "bool"
        result = "float" if "float" in (ltag, rtag) else "int"
        if op == "/" and result == "int":
            return f"ax_idiv({lcode}, {rcode})", "int"
        if op == "%" and result == "int":
            return f"ax_imod({lcode}, {rcode})", "int"
        if op in {"<<", ">>", "&", "|", "^"}:
            return f"({lcode} {op} {rcode})", "int"
        return f"({lcode} {op} {rcode})", result

    def _emit_unary(self, node: ast.UnaryExpr, ctx: FnContext) -> tuple[str, str]:
        code, tag = self._emit_expr(node.operand, ctx)
        if node.op == "not":
            return f"(!{code})", "bool"
        if node.op == "*":
            return code, tag
        return f"({node.op}{code})", tag

    def _emit_call(self, node: ast.CallExpr, ctx: FnContext) -> tuple[str, str]:
        callee = node.callee
        if isinstance(callee, ast.NameExpr):
            name = callee.name
            if name in self.functions:
                fn = self.functions[name]
                args = ", ".join(self._emit_expr(a.value, ctx)[0] for a in node.args)
                return f"{name}({args})", self.type_tag(fn.return_type)
            raise unsupported(f"native backend cannot call `{name}`", node.span)
        if isinstance(callee, ast.FieldExpr):
            return self._emit_method_call(callee, node.args, ctx)
        raise unsupported("native backend does not support this call form", node.span)

    def _emit_method_call(self, field: ast.FieldExpr, args: list[ast.Arg], ctx: FnContext) -> tuple[str, str]:
        recv = field.recv
        mname = field.name
        # static call on a known type, e.g. Circle.new(...)
        if isinstance(recv, ast.NameExpr) and recv.name == "Math":
            return self._emit_math(mname, args, ctx)
        if isinstance(recv, ast.NameExpr) and recv.name in self.methods:
            table = self.methods[recv.name]
            if mname in table and not (table[mname].params and table[mname].params[0].is_self):
                arglist = ", ".join(self._emit_expr(a.value, ctx)[0] for a in args)
                return f"{recv.name}__{mname}({arglist})", self.type_tag(table[mname].return_type)
        rcode, rtag = self._emit_expr(recv, ctx)
        if rtag == "str":
            return self._emit_str_method(rcode, mname, args, ctx)
        table = self.methods.get(rtag)
        if table and mname in table:
            m = table[mname]
            arglist = [self._emit_expr(a.value, ctx)[0] for a in args]
            if m.params and m.params[0].is_self and m.params[0].is_mut:
                recv_arg = f"&({rcode})"
            else:
                recv_arg = rcode
            joined = ", ".join([recv_arg, *arglist])
            return f"{rtag}__{mname}({joined})", self.type_tag(m.return_type)
        raise unsupported(
            f"native backend cannot resolve method `{mname}` on `{rtag}`",
            field.span,
        )

    def _emit_str_method(self, rcode: str, mname: str, args, ctx) -> tuple[str, str]:
        if mname == "len":
            return f"ax_str_len({rcode})", "int"
        if mname == "compare" and args:
            other, _ = self._emit_expr(args[0].value, ctx)
            return f"ax_str_cmp({rcode}, {other})", "int"
        raise unsupported(f"native backend does not support str.{mname} yet")

    def _emit_math(self, mname: str, args, ctx) -> tuple[str, str]:
        cmap = {"sqrt": "sqrt", "sin": "sin", "cos": "cos", "tan": "tan",
                "log": "log", "exp": "exp", "floor": "floor", "ceil": "ceil",
                "pow": "pow"}
        if mname in cmap:
            arglist = ", ".join(self._emit_expr(a.value, ctx)[0] for a in args)
            return f"{cmap[mname]}({arglist})", "float"
        raise unsupported(f"native backend does not support Math.{mname}")

    def _emit_field(self, node: ast.FieldExpr, ctx: FnContext) -> tuple[str, str]:
        if isinstance(node.recv, ast.NameExpr) and node.recv.name == "Math":
            consts = {"PI": "3.141592653589793", "E": "2.718281828459045",
                      "TAU": "6.283185307179586"}
            if node.name in consts:
                return consts[node.name], "float"
        code, tag = self._emit_expr(node.recv, ctx)
        if tag in self.struct_fields and node.name in self.struct_fields[tag]:
            return f"({code}).{node.name}", self.struct_fields[tag][node.name]
        raise unsupported(
            f"native backend cannot access field `{node.name}` on `{tag}`",
            node.span,
        )

    def _emit_record(self, node: ast.RecordLit, ctx: FnContext) -> tuple[str, str]:
        name = node.type_name
        if name not in self.structs:
            raise unsupported(f"native backend cannot construct `{name}`", node.span)
        provided = {f: v for f, v in node.fields}
        inits: list[str] = []
        for f in self.structs[name].fields:
            if f.name in provided:
                code, _ = self._emit_expr(provided[f.name], ctx)
                inits.append(f".{f.name} = {code}")
            elif f.default is not None:
                code, _ = self._emit_expr(f.default, ctx)
                inits.append(f".{f.name} = {code}")
        body = ", ".join(inits)
        return f"(({name}){{{body}}})", name

    def _emit_fstr(self, node: ast.FStr, ctx: FnContext) -> str:
        pieces: list[str] = []
        for part in node.parts:
            if isinstance(part, str):
                pieces.append(f"ax_str_lit({self._c_string(part)})")
            else:
                pieces.append(self._to_str(*self._emit_expr(part, ctx)))
        if not pieces:
            return 'ax_str_lit("")'
        acc = pieces[0]
        for p in pieces[1:]:
            acc = f"ax_str_concat({acc}, {p})"
        return acc

    def _to_str(self, code: str, tag: str) -> str:
        if tag == "str":
            return code
        if tag == "int":
            return f"ax_fmt_int({code})"
        if tag == "float":
            return f"ax_fmt_float({code})"
        if tag == "bool":
            return f"ax_fmt_bool({code})"
        raise unsupported(f"native backend cannot format a value of type `{tag}`")

    def _c_string(self, s: str) -> str:
        out = ['"']
        for ch in s:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                out.append("\\r")
            elif 32 <= ord(ch) < 127:
                out.append(ch)
            else:
                for b in ch.encode("utf-8"):
                    out.append(f"\\x{b:02x}")
        out.append('"')
        return "".join(out)


def generate_c(module: ast.Module) -> str:
    return CBackend(module).generate()
