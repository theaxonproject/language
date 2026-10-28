"""AXON native backend: AST -> C11.

Lowers the statically-typed core of the AXON language to portable C11. The
emitted C is compiled by the host C compiler into a genuine native executable
with no interpreter dependency.

Natively compiled today: functions and recursion, the scalar types, structs
with methods and trait methods (static dispatch), enums (C-style and with
payloads, as tagged unions), `Option`/`Result` with `try`/`?` propagation and
`catch` handlers, `match` with guards and or-patterns, `if let`, `Vec` and
`Map(str, V)` collections (monomorphized), global constants, the string
method library, `if`/`for`/`while`/`loop` control flow, f-strings, casts, and
the core I/O and panic intrinsics.

Constructs outside this core (closures, generics, tasks/channels, the
networked/JSON standard library) raise a clear E800 diagnostic; the driver
then falls back to a portable artifact that runs through the interpreter.

Type tags
---------
Expression types are tracked as *tags*: plain strings for scalars and user
structs ("int", "float", "bool", "str", "void", "Point", ...) and tuples for
composites: ("enum", Name), ("opt", T), ("res", Ok, Err), ("vec", T),
("map", V). Composite runtime types are monomorphized on demand and emitted
in dependency order.
"""

from __future__ import annotations

import copy
import pathlib
import re

from . import ast
from .stdlib import BUILTIN_FUNCTIONS
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
        "run the program with `axc run --interpret` instead",
    )


def mangle(tag) -> str:
    """A C-identifier-safe suffix for a type tag."""
    if isinstance(tag, str):
        return tag
    kind = tag[0]
    if kind in {"fn", "tup"}:
        # variable arity: encode it so nested signatures cannot collide
        kind = f"{kind}{len(tag) - 1}"
    return kind + "_" + "_".join(mangle(t) for t in tag[1:])


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
    def __init__(self, self_type=None, self_ptr: bool = False, ret_tag="void") -> None:
        self.var_types: dict[str, object] = {}
        self.mut_vars: set[str] = set()
        self.self_type = self_type
        self.self_ptr = self_ptr
        self.ret_tag = ret_tag
        # block-scoped `defer`: one list of deferred expressions per open
        # block, plus the scope depth at which each enclosing loop body starts
        self.defer_scopes: list[list[ast.Node]] = []
        self.loop_marks: list[int] = []
        self._tmp = 0

    def tmp(self) -> str:
        self._tmp += 1
        return f"__ax{self._tmp}"

    def has_defers(self, start: int = 0) -> bool:
        return any(self.defer_scopes[start:])


_LITERAL_RE = re.compile(
    r'^(-?\d+(\.\d+)?([eE][-+]?\d+)?|INT64_C\(-?\d+\)|true|false|'
    r'ax_str_lit\("([^"\\]|\\.)*"\))$')


class CBackend:
    def __init__(self, module: ast.Module) -> None:
        self.module = module
        self.structs: dict[str, ast.StructDef] = {}
        self.enums: dict[str, ast.EnumDef] = {}
        self.enum_variant_owner: dict[str, str] = {}
        self.struct_fields: dict[str, dict[str, object]] = {}
        self.functions: dict[str, ast.FnDef] = {}
        self.methods: dict[str, dict[str, ast.FnDef]] = {}
        self.consts: dict[str, ast.Const] = {}
        self.const_tags: dict[str, object] = {}
        # monomorphized/declared types, in dependency order
        self.type_defs: list[str] = []
        self._ensured: set = set()
        self._shows: dict = {}
        # closures and function references, emitted after the prototypes
        self.lambda_defs: list[str] = []
        self._lambda_count = 0
        self._fnrefs: set[str] = set()
        self._helpers: set = set()
        # generic functions: type-parameter bindings in scope while emitting a
        # specialization, plus the specializations requested so far
        self._tenv: dict[str, object] = {}
        self._specs: dict[tuple, str] = {}
        self._pending_specs: list[tuple] = []
        self.spec_protos: list[str] = []
        self._collect()

    def _collect(self) -> None:
        for item in self.module.items:
            if isinstance(item, ast.StructDef) and item.name != "__extern__":
                self.structs[item.name] = item
            elif isinstance(item, ast.EnumDef):
                self.enums[item.name] = item
                for v in item.variants:
                    self.enum_variant_owner.setdefault(v.name, item.name)
            elif isinstance(item, ast.FnDef):
                self.functions[item.name] = item
            elif isinstance(item, ast.ImplBlock):
                table = self.methods.setdefault(item.type_name, {})
                for m in item.methods:
                    table[m.name] = m
            elif isinstance(item, ast.Const):
                self.consts[item.name] = item
        # trait default methods are compiled for every implementing type
        defaults = {item.name: [m for m in item.methods if m.body is not None]
                    for item in self.module.items if isinstance(item, ast.TraitDef)}
        for item in self.module.items:
            if isinstance(item, ast.ImplBlock) and item.trait_name in defaults:
                table = self.methods.setdefault(item.type_name, {})
                for m in defaults[item.trait_name]:
                    table.setdefault(m.name, m)

    # -- type machinery -------------------------------------------------------
    def type_tag(self, node: ast.Node | None):
        if node is None:
            return "void"
        if isinstance(node, ast.TypeName):
            n = node.name
            if n in self._tenv and not node.args:
                return self._tenv[n]
            if n in _INT_TYPES:
                return "int"
            if n in _FLOAT_TYPES:
                return "float"
            if n in {"bool", "str", "void"}:
                return n
            if n in self.structs:
                return n
            if n in self.enums:
                return ("enum", n)
            if n == "Option" and len(node.args) == 1:
                return ("opt", self.type_tag(node.args[0]))
            if n == "Result" and node.args:
                errt = self.type_tag(node.args[1]) if len(node.args) > 1 else "str"
                return ("res", self.type_tag(node.args[0]), errt)
            if n == "Vec" and len(node.args) == 1:
                return ("vec", self.type_tag(node.args[0]))
            if n == "Map" and len(node.args) == 2:
                key = self.type_tag(node.args[0])
                if key != "str":
                    raise unsupported("native Map keys must be `str`", node.span)
                return ("map", self.type_tag(node.args[1]))
            raise unsupported(f"native backend does not support type `{n}`", node.span)
        if isinstance(node, ast.OptionalType):
            return ("opt", self.type_tag(node.inner))
        if isinstance(node, ast.ResultType):
            errt = self.type_tag(node.err) if node.err is not None else "str"
            return ("res", self.type_tag(node.ok), errt)
        if isinstance(node, ast.RefType):
            return self.type_tag(node.inner)
        if isinstance(node, ast.SliceType):
            return ("vec", self.type_tag(node.elem))
        if isinstance(node, ast.ArrayType):
            return ("vec", self.type_tag(node.elem))
        if isinstance(node, ast.TupleType):
            if not node.elems:
                return "void"
            return ("tup", *[self.type_tag(e) for e in node.elems])
        if isinstance(node, ast.FnType):
            ret = self.type_tag(node.ret) if node.ret is not None else "void"
            return ("fn", ret, *[self.type_tag(p) for p in node.params])
        raise unsupported("native backend does not support this type form",
                          getattr(node, "span", None))

    def ctype(self, tag) -> str:
        if isinstance(tag, str):
            base = {
                "int": "int64_t", "float": "double", "bool": "bool",
                "str": "AxStr", "void": "void",
                "file": "AxStr",  # a checked file path
            }.get(tag)
            if base is not None:
                return base
            if tag in self.structs:
                return tag
            raise unsupported(f"native backend has no representation for `{tag}`")
        kind = tag[0]
        if kind == "enum":
            return tag[1]
        if kind == "opt":
            return f"AxOpt_{mangle(tag[1])}"
        if kind == "res":
            return f"AxRes_{mangle(tag[1])}_{mangle(tag[2])}"
        if kind == "vec":
            return f"AxVec_{mangle(tag[1])}"
        if kind == "map":
            return f"AxMap_{mangle(tag[1])}"
        if kind in {"tup", "fn"}:
            return f"AxT_{mangle(tag)}"
        raise unsupported(f"native backend has no representation for `{tag}`")

    # -- monomorphization -------------------------------------------------------
    def ensure(self, tag) -> None:
        """Emit the definition for a composite tag (and its dependencies)."""
        key = tag
        if key in self._ensured:
            return
        if isinstance(tag, str):
            if tag in self.structs:
                self._ensured.add(key)
                self._emit_struct_def(self.structs[tag])
            return
        self._ensured.add(key)
        kind = tag[0]
        if kind == "enum":
            self._emit_enum_def(self.enums[tag[1]])
        elif kind == "opt":
            self.ensure(tag[1])
            self._emit_opt_def(tag)
        elif kind == "res":
            self.ensure(tag[1])
            self.ensure(tag[2])
            self._emit_res_def(tag)
        elif kind == "vec":
            self.ensure(tag[1])
            self.ensure(("opt", tag[1]))
            self._emit_vec_def(tag)
        elif kind == "map":
            self.ensure(tag[1])
            self.ensure(("opt", tag[1]))
            self.ensure(("vec", "str"))
            self.ensure(("vec", tag[1]))
            self._emit_map_def(tag)
        elif kind == "tup":
            for t in tag[1:]:
                self.ensure(t)
            fields = " ".join(f"{self.ctype(t)} f{i};" for i, t in enumerate(tag[1:]))
            self.type_defs.append(f"typedef struct {{ {fields} }} {self.ctype(tag)};")
        elif kind == "fn":
            # a closure value: environment pointer + code pointer
            for t in tag[1:]:
                if t != "void":
                    self.ensure(t)
            params = ", ".join(["void *", *[self.ctype(t) for t in tag[2:]]])
            self.type_defs.append(
                f"typedef struct {{ void *env; {self.ctype(tag[1])} (*call)({params}); }} "
                f"{self.ctype(tag)};")

    def _emit_struct_def(self, sdef: ast.StructDef) -> None:
        if sdef.generics:
            raise unsupported(
                f"native backend does not support generic struct `{sdef.name}`",
                sdef.span,
            )
        fields = []
        tags: dict[str, object] = {}
        for f in sdef.fields:
            tag = self.type_tag(f.type)
            self.ensure(tag)
            tags[f.name] = tag
            fields.append(f"    {self.ctype(tag)} {cfield(f.name)};")
        self.struct_fields[sdef.name] = tags
        body = "\n".join(fields)
        self.type_defs.append(
            f"typedef struct {sdef.name} {{\n{body}\n}} {sdef.name};"
        )

    def _enum_has_payload(self, edef: ast.EnumDef) -> bool:
        return any(v.payload for v in edef.variants)

    def _emit_enum_def(self, edef: ast.EnumDef) -> None:
        if edef.generics:
            raise unsupported(
                f"native backend does not support generic enum `{edef.name}`",
                edef.span,
            )
        name = edef.name
        tag_consts = ", ".join(
            f"{name}__{v.name} = {i}" for i, v in enumerate(edef.variants)
        )
        if not self._enum_has_payload(edef):
            self.type_defs.append(f"typedef enum {{ {tag_consts} }} {name};")
            return
        # tagged union
        members: list[str] = []
        ctors: list[str] = []
        for v in edef.variants:
            if not v.payload:
                ctors.append(
                    f"static {name} {name}_mk_{v.name}(void) {{\n"
                    f"    {name} e; e.tag = {name}__{v.name}; return e;\n}}"
                )
                continue
            fields = []
            params = []
            assigns = []
            for i, (fname, ftype) in enumerate(v.payload):
                ftag = self.type_tag(ftype)
                self.ensure(ftag)
                cname = cfield(fname) if fname else f"f{i}"
                fields.append(f"        {self.ctype(ftag)} {cname};")
                params.append(f"{self.ctype(ftag)} {cname}")
                assigns.append(f"    e.as_.{v.name}.{cname} = {cname};")
            members.append(
                "    struct {\n" + "\n".join("    " + f for f in fields)
                + f"\n    }} {v.name};"
            )
            ctors.append(
                f"static {name} {name}_mk_{v.name}({', '.join(params)}) {{\n"
                f"    {name} e; e.tag = {name}__{v.name};\n"
                + "\n".join(assigns) + "\n    return e;\n}"
            )
        union = "\n".join(members)
        self.type_defs.append(
            f"enum {{ {tag_consts} }};\n"
            f"typedef struct {name} {{\n    int32_t tag;\n"
            f"    union {{\n{union}\n    }} as_;\n}} {name};\n"
            + "\n".join(ctors)
        )

    def _emit_opt_def(self, tag) -> None:
        s = mangle(tag[1])
        inner = tag[1]
        t = self.ctype(inner)
        c = self.ctype(tag)
        self.type_defs.append(
            f"typedef struct {{ bool has; {t} val; }} {c};\n"
            f"static {c} ax_opt_some_{s}({t} v) {{ {c} o; o.has = true; o.val = v; return o; }}\n"
            f"static {c} ax_opt_none_{s}(void) {{ {c} o; memset(&o, 0, sizeof(o)); o.has = false; return o; }}\n"
            f"static {t} ax_opt_unwrap_{s}({c} o) {{\n"
            f"    if (!o.has) ax_panic(ax_str_lit(\"called unwrap on a none value\"));\n"
            f"    return o.val;\n}}\n"
            f"static {t} ax_opt_expect_{s}({c} o, AxStr msg) {{\n"
            f"    if (!o.has) ax_panic(msg);\n    return o.val;\n}}"
        )

    def _emit_res_def(self, tag) -> None:
        s = mangle(tag)
        tok, terr = self.ctype(tag[1]), self.ctype(tag[2])
        c = self.ctype(tag)
        show_err = self.ensure_show(tag[2])
        self.type_defs.append(
            f"typedef struct {{ bool ok; {tok} val; {terr} err; }} {c};\n"
            f"static {c} ax_res_ok_{s}({tok} v) {{ {c} r; memset(&r, 0, sizeof(r)); r.ok = true; r.val = v; return r; }}\n"
            f"static {c} ax_res_err_{s}({terr} e) {{ {c} r; memset(&r, 0, sizeof(r)); r.ok = false; r.err = e; return r; }}\n"
            f"static {tok} ax_res_unwrap_{s}({c} r) {{\n"
            f"    if (!r.ok) ax_panic(ax_str_concat(ax_str_lit(\"called unwrap on an Err value: \"), {show_err}(r.err)));\n"
            f"    return r.val;\n}}\n"
            f"static {terr} ax_res_unwrap_err_{s}({c} r) {{\n"
            f"    if (r.ok) ax_panic(ax_str_lit(\"called unwrap_err on an Ok value\"));\n"
            f"    return r.err;\n}}"
        )

    def _emit_vec_def(self, tag) -> None:
        elem = tag[1]
        s = mangle(elem)
        t = self.ctype(elem)
        c = self.ctype(tag)
        opt_c = self.ctype(("opt", elem))
        parts = [
            f"typedef struct {{ {t} *data; int64_t len; int64_t cap; }} {c};",
            f"static {c} ax_vec_new_{s}(void) {{ {c} v; v.data = NULL; v.len = 0; v.cap = 0; return v; }}",
            f"static void ax_vec_push_{s}({c} *v, {t} x) {{\n"
            f"    if (v->len == v->cap) {{\n"
            f"        v->cap = v->cap < 4 ? 4 : v->cap * 2;\n"
            f"        v->data = ({t} *)ax_realloc(v->data, (size_t)v->cap * sizeof({t}));\n"
            f"    }}\n    v->data[v->len++] = x;\n}}",
            f"static int64_t ax_vec_idx_{s}({c} v, int64_t i) {{\n"
            f"    if (i < 0) i += v.len;\n"
            f"    if (i < 0 || i >= v.len) ax_panic(ax_str_lit(\"index out of range\"));\n"
            f"    return i;\n}}",
            f"static {t} ax_vec_get_{s}({c} v, int64_t i) {{ return v.data[ax_vec_idx_{s}(v, i)]; }}",
            f"static void ax_vec_set_{s}({c} *v, int64_t i, {t} x) {{ v->data[ax_vec_idx_{s}(*v, i)] = x; }}",
            f"static {opt_c} ax_vec_pop_{s}({c} *v) {{\n"
            f"    if (v->len == 0) return ax_opt_none_{s}();\n"
            f"    return ax_opt_some_{s}(v->data[--v->len]);\n}}",
            f"static {opt_c} ax_vec_first_{s}({c} v) {{\n"
            f"    return v.len ? ax_opt_some_{s}(v.data[0]) : ax_opt_none_{s}();\n}}",
            f"static {opt_c} ax_vec_last_{s}({c} v) {{\n"
            f"    return v.len ? ax_opt_some_{s}(v.data[v.len - 1]) : ax_opt_none_{s}();\n}}",
            f"static {opt_c} ax_vec_get_opt_{s}({c} v, int64_t i) {{\n"
            f"    return (i >= 0 && i < v.len) ? ax_opt_some_{s}(v.data[i]) : ax_opt_none_{s}();\n}}",
            f"static {c} ax_vec_reverse_{s}({c} v) {{\n"
            f"    {c} out = ax_vec_new_{s}();\n"
            f"    for (int64_t i = v.len - 1; i >= 0; i--) ax_vec_push_{s}(&out, v.data[i]);\n"
            f"    return out;\n}}",
            f"static void ax_vec_insert_{s}({c} *v, int64_t i, {t} x) {{\n"
            f"    if (i < 0) i += v->len;\n"
            f"    if (i < 0 || i > v->len) ax_panic(ax_str_lit(\"index out of range\"));\n"
            f"    ax_vec_push_{s}(v, x);\n"
            f"    memmove(v->data + i + 1, v->data + i, (size_t)(v->len - 1 - i) * sizeof({t}));\n"
            f"    v->data[i] = x;\n}}",
            f"static {t} ax_vec_remove_{s}({c} *v, int64_t i) {{\n"
            f"    i = ax_vec_idx_{s}(*v, i);\n"
            f"    {t} x = v->data[i];\n"
            f"    memmove(v->data + i, v->data + i + 1, (size_t)(v->len - 1 - i) * sizeof({t}));\n"
            f"    v->len--;\n    return x;\n}}",
            f"static {c} ax_vec_slice_{s}({c} v, int64_t start, int64_t end) {{\n"
            f"    if (start < 0) start += v.len;\n    if (end < 0) end += v.len;\n"
            f"    if (start < 0) start = 0;\n    if (end > v.len) end = v.len;\n"
            f"    {c} out = ax_vec_new_{s}();\n"
            f"    for (int64_t i = start; i < end; i++) ax_vec_push_{s}(&out, v.data[i]);\n"
            f"    return out;\n}}",
        ]
        eq = self._eq_fn(elem)
        if eq is not None:
            parts.append(
                f"static bool ax_vec_contains_{s}({c} v, {t} x) {{\n"
                f"    for (int64_t i = 0; i < v.len; i++) if ({eq('v.data[i]', 'x')}) return true;\n"
                f"    return false;\n}}"
            )
        if elem in {"int", "float"}:
            add = "ax_iadd(acc, v.data[i])" if elem == "int" else "acc + v.data[i]"
            parts.append(
                f"static {t} ax_vec_sum_{s}({c} v) {{\n"
                f"    {t} acc = 0;\n"
                f"    for (int64_t i = 0; i < v.len; i++) acc = {add};\n"
                f"    return acc;\n}}"
            )
        if elem in {"int", "float", "str"}:
            cmp = {
                "int": "return (*a > *b) - (*a < *b);",
                "float": "return (*a > *b) - (*a < *b);",
                "str": "return ax_str_cmp(*a, *b);",
            }[elem]
            parts.append(
                f"static int ax_vec_cmp_{s}(const void *pa, const void *pb) {{\n"
                f"    const {t} *a = (const {t} *)pa; const {t} *b = (const {t} *)pb;\n"
                f"    {cmp}\n}}\n"
                f"static {c} ax_vec_sort_{s}({c} v) {{\n"
                f"    {c} out = ax_vec_slice_{s}(v, 0, v.len);\n"
                f"    qsort(out.data, (size_t)out.len, sizeof({t}), ax_vec_cmp_{s});\n"
                f"    return out;\n}}"
            )
        if elem == "str":
            parts.extend([
                f"static {c} ax_str_split(AxStr str_, AxStr sep) {{\n"
                f"    {c} out = ax_vec_new_str();\n"
                f"    if (sep.len == 0) {{ ax_vec_push_str(&out, str_); return out; }}\n"
                f"    int64_t start = 0;\n"
                f"    for (int64_t i = 0; i + sep.len <= str_.len;) {{\n"
                f"        if (memcmp(str_.data + i, sep.data, (size_t)sep.len) == 0) {{\n"
                f"            ax_vec_push_str(&out, ax_str_slice(str_, start, i));\n"
                f"            i += sep.len; start = i;\n"
                f"        }} else i++;\n"
                f"    }}\n"
                f"    ax_vec_push_str(&out, ax_str_slice(str_, start, str_.len));\n"
                f"    return out;\n}}",
                f"static {c} ax_str_split_ws(AxStr str_) {{\n"
                f"    {c} out = ax_vec_new_str();\n"
                f"    int64_t i = 0;\n"
                f"    while (i < str_.len) {{\n"
                f"        while (i < str_.len && ax_is_space(str_.data[i])) i++;\n"
                f"        int64_t start = i;\n"
                f"        while (i < str_.len && !ax_is_space(str_.data[i])) i++;\n"
                f"        if (i > start) ax_vec_push_str(&out, ax_str_slice(str_, start, i));\n"
                f"    }}\n    return out;\n}}",
                f"static {c} ax_str_lines(AxStr str_) {{\n"
                f"    {c} out = ax_vec_new_str();\n"
                f"    int64_t start = 0;\n"
                f"    for (int64_t i = 0; i < str_.len; i++) {{\n"
                f"        if (str_.data[i] == '\\n') {{\n"
                f"            int64_t end = i;\n"
                f"            if (end > start && str_.data[end - 1] == '\\r') end--;\n"
                f"            ax_vec_push_str(&out, ax_str_slice(str_, start, end));\n"
                f"            start = i + 1;\n"
                f"        }}\n    }}\n"
                f"    if (start < str_.len) ax_vec_push_str(&out, ax_str_slice(str_, start, str_.len));\n"
                f"    return out;\n}}",
                f"static {c} ax_str_chars(AxStr str_) {{\n"
                f"    {c} out = ax_vec_new_str();\n"
                f"    int64_t i = 0;\n"
                f"    while (i < str_.len) {{\n"
                f"        int64_t n = 1;\n"
                f"        unsigned char b = (unsigned char)str_.data[i];\n"
                f"        if ((b & 0xF8) == 0xF0) n = 4;\n"
                f"        else if ((b & 0xF0) == 0xE0) n = 3;\n"
                f"        else if ((b & 0xE0) == 0xC0) n = 2;\n"
                f"        if (i + n > str_.len) n = 1;\n"
                f"        ax_vec_push_str(&out, ax_str_slice(str_, i, i + n));\n"
                f"        i += n;\n    }}\n    return out;\n}}",
            ])
        self.type_defs.append("\n".join(parts))

    def _emit_map_def(self, tag) -> None:
        val = tag[1]
        s = mangle(val)
        t = self.ctype(val)
        c = self.ctype(tag)
        opt_c = self.ctype(("opt", val))
        vec_str = self.ctype(("vec", "str"))
        vec_val = self.ctype(("vec", val))
        self.type_defs.append("\n".join([
            f"typedef struct {{ {vec_str} keys; {vec_val} vals; }} {c};",
            f"static {c} ax_map_new_{s}(void) {{\n"
            f"    {c} m; m.keys = ax_vec_new_str(); m.vals = ax_vec_new_{s}(); return m;\n}}",
            f"static int64_t ax_map_find_{s}({c} m, AxStr k) {{\n"
            f"    for (int64_t i = 0; i < m.keys.len; i++)\n"
            f"        if (ax_str_eq(m.keys.data[i], k)) return i;\n"
            f"    return -1;\n}}",
            f"static void ax_map_insert_{s}({c} *m, AxStr k, {t} v) {{\n"
            f"    int64_t i = ax_map_find_{s}(*m, k);\n"
            f"    if (i >= 0) m->vals.data[i] = v;\n"
            f"    else {{ ax_vec_push_str(&m->keys, k); ax_vec_push_{s}(&m->vals, v); }}\n}}",
            f"static {opt_c} ax_map_get_{s}({c} m, AxStr k) {{\n"
            f"    int64_t i = ax_map_find_{s}(m, k);\n"
            f"    return i >= 0 ? ax_opt_some_{s}(m.vals.data[i]) : ax_opt_none_{s}();\n}}",
            f"static {t} ax_map_index_{s}({c} m, AxStr k) {{\n"
            f"    int64_t i = ax_map_find_{s}(m, k);\n"
            f"    if (i < 0) ax_panic(ax_str_concat(ax_str_lit(\"missing map key: \"), k));\n"
            f"    return m.vals.data[i];\n}}",
            f"static bool ax_map_contains_{s}({c} m, AxStr k) {{ return ax_map_find_{s}(m, k) >= 0; }}",
            f"static {opt_c} ax_map_remove_{s}({c} *m, AxStr k) {{\n"
            f"    int64_t i = ax_map_find_{s}(*m, k);\n"
            f"    if (i < 0) return ax_opt_none_{s}();\n"
            f"    {t} v = m->vals.data[i];\n"
            f"    for (int64_t j = i; j + 1 < m->keys.len; j++) {{\n"
            f"        m->keys.data[j] = m->keys.data[j + 1];\n"
            f"        m->vals.data[j] = m->vals.data[j + 1];\n"
            f"    }}\n    m->keys.len--; m->vals.len--;\n"
            f"    return ax_opt_some_{s}(v);\n}}",
        ]))

    def _eq_fn(self, tag):
        """Return fn(a, b) -> C equality expression, or None when unsupported."""
        if tag in {"int", "float", "bool"}:
            return lambda a, b: f"({a} == {b})"
        if tag == "str":
            return lambda a, b: f"ax_str_eq({a}, {b})"
        if isinstance(tag, tuple) and tag[0] == "enum" and \
                not self._enum_has_payload(self.enums[tag[1]]):
            return lambda a, b: f"({a} == {b})"
        if isinstance(tag, tuple) and tag[0] == "tup":
            parts = [self._eq_fn(t) for t in tag[1:]]
            if all(p is not None for p in parts):
                return lambda a, b: "(" + " && ".join(
                    p(f"({a}).f{i}", f"({b}).f{i}") for i, p in enumerate(parts)) + ")"
        if isinstance(tag, tuple) and tag[0] == "opt":
            inner = self._eq_fn(tag[1])
            if inner is not None:
                return lambda a, b: (f"(({a}).has == ({b}).has && (!({a}).has || "
                                     f"{inner(f'({a}).val', f'({b}).val')}))")
        return None

    # -- show (to-string) functions --------------------------------------------
    def ensure_show(self, tag) -> str:
        """Emit (once) and return the name of a to-string function for `tag`."""
        if tag == "int":
            return "ax_fmt_int"
        if tag == "float":
            return "ax_fmt_float"
        if tag == "bool":
            return "ax_fmt_bool"
        if tag == "str":
            return "ax_show_str_quoted"
        key = ("show", tag)
        if key in self._shows:
            return self._shows[key]
        self.ensure(tag)
        name = f"ax_show_{mangle(tag)}"
        self._shows[key] = name
        c = self.ctype(tag)
        w = Writer()
        if isinstance(tag, str) and tag in self.structs:
            w.line(f"static AxStr {name}({c} v) {{")
            w.indent()
            w.line(f'AxStr out = ax_str_lit("{tag}{{ ");')
            first = True
            for fname, ftag in self.struct_fields[tag].items():
                inner_show = self.ensure_show(ftag)
                sep = "" if first else ", "
                w.line(f'out = ax_str_concat(out, ax_str_lit("{sep}{fname}: "));')
                w.line(f"out = ax_str_concat(out, {inner_show}(v.{cfield(fname)}));")
                first = False
            w.line('return ax_str_concat(out, ax_str_lit(" }"));')
            w.dedent()
            w.line("}")
        elif tag[0] == "enum":
            edef = self.enums[tag[1]]
            ename = tag[1]
            w.line(f"static AxStr {name}({c} v) {{")
            w.indent()
            access = "v.tag" if self._enum_has_payload(edef) else "v"
            w.line(f"switch ({access}) {{")
            for v in edef.variants:
                w.line(f"case {ename}__{v.name}: {{")
                w.indent()
                if not v.payload:
                    w.line(f'return ax_str_lit("{v.name}");')
                else:
                    w.line(f'AxStr out = ax_str_lit("{v.name}(");')
                    for i, (fname, ftype) in enumerate(v.payload):
                        cname = cfield(fname) if fname else f"f{i}"
                        inner_show = self.ensure_show(self.type_tag(ftype))
                        if i:
                            w.line('out = ax_str_concat(out, ax_str_lit(", "));')
                        w.line(f"out = ax_str_concat(out, {inner_show}(v.as_.{v.name}.{cname}));")
                    w.line('return ax_str_concat(out, ax_str_lit(")"));')
                w.dedent()
                w.line("}")
            w.line("}")
            w.line('return ax_str_lit("?");')
            w.dedent()
            w.line("}")
        elif tag[0] == "opt":
            inner_show = self.ensure_show(tag[1])
            w.line(f"static AxStr {name}({c} v) {{")
            w.indent()
            w.line('if (!v.has) return ax_str_lit("none");')
            w.line(f'return ax_str_concat(ax_str_concat(ax_str_lit("Some("), {inner_show}(v.val)), ax_str_lit(")"));')
            w.dedent()
            w.line("}")
        elif tag[0] == "res":
            ok_show = self.ensure_show(tag[1])
            err_show = self.ensure_show(tag[2])
            w.line(f"static AxStr {name}({c} v) {{")
            w.indent()
            w.line(f'if (v.ok) return ax_str_concat(ax_str_concat(ax_str_lit("Ok("), {ok_show}(v.val)), ax_str_lit(")"));')
            w.line(f'return ax_str_concat(ax_str_concat(ax_str_lit("Err("), {err_show}(v.err)), ax_str_lit(")"));')
            w.dedent()
            w.line("}")
        elif tag[0] == "vec":
            inner_show = self.ensure_show(tag[1])
            w.line(f"static AxStr {name}({c} v) {{")
            w.indent()
            w.line('AxStr out = ax_str_lit("[");')
            w.line("for (int64_t i = 0; i < v.len; i++) {")
            w.indent()
            w.line('if (i) out = ax_str_concat(out, ax_str_lit(", "));')
            w.line(f"out = ax_str_concat(out, {inner_show}(v.data[i]));")
            w.dedent()
            w.line("}")
            w.line('return ax_str_concat(out, ax_str_lit("]"));')
            w.dedent()
            w.line("}")
        elif tag[0] == "tup":
            w.line(f"static AxStr {name}({c} v) {{")
            w.indent()
            w.line('AxStr out = ax_str_lit("(");')
            for i, t in enumerate(tag[1:]):
                inner_show = self.ensure_show(t)
                if i:
                    w.line('out = ax_str_concat(out, ax_str_lit(", "));')
                w.line(f"out = ax_str_concat(out, {inner_show}(v.f{i}));")
            w.line('return ax_str_concat(out, ax_str_lit(")"));')
            w.dedent()
            w.line("}")
        elif tag[0] == "map":
            inner_show = self.ensure_show(tag[1])
            w.line(f"static AxStr {name}({c} v) {{")
            w.indent()
            w.line('AxStr out = ax_str_lit("{");')
            w.line("for (int64_t i = 0; i < v.keys.len; i++) {")
            w.indent()
            w.line('if (i) out = ax_str_concat(out, ax_str_lit(", "));')
            w.line("out = ax_str_concat(out, v.keys.data[i]);")
            w.line('out = ax_str_concat(out, ax_str_lit(": "));')
            w.line(f"out = ax_str_concat(out, {inner_show}(v.vals.data[i]));")
            w.dedent()
            w.line("}")
            w.line('return ax_str_concat(out, ax_str_lit("}"));')
            w.dedent()
            w.line("}")
        else:
            raise unsupported(f"native backend cannot format a value of type `{tag}`")
        self.type_defs.append(w.text().rstrip())
        return name

    # -- top-level generation -------------------------------------------------
    def generate(self) -> str:
        # user type declarations, in declaration order
        for name in self.structs:
            self.ensure(name)
        for name in self.enums:
            self.ensure(("enum", name))

        # constants
        consts_w = Writer()
        init_w = Writer()
        init_w.line("static void ax_init_consts(void) {")
        init_w.indent()
        const_ctx = FnContext()
        for name, item in self.consts.items():
            body = Writer()
            code, tag = self._emit_expr(item.value, const_ctx, body)
            if item.type is not None:
                tag = self.type_tag(item.type)
            self.const_tags[name] = tag
            consts_w.line(f"static {self.ctype(tag)} {name};")
            for line in body.parts:
                init_w.parts.append("    " + line)
            init_w.line(f"{name} = {code};")
        init_w.dedent()
        init_w.line("}")

        # prototypes and bodies
        protos = Writer()
        bodies = Writer()
        for fn in self.functions.values():
            if not fn.generics:
                protos.line(self._prototype(fn) + ";")
        for owner, table in self.methods.items():
            for m in table.values():
                protos.line(self._prototype(m, owner) + ";")
        for fn in self.functions.values():
            if not fn.generics:
                self._emit_fn(bodies, fn)
        for owner, table in self.methods.items():
            for m in table.values():
                self._emit_fn(bodies, m, owner)
        # generic specializations requested by the bodies (and by each other)
        while self._pending_specs:
            fn, bindings, cname = self._pending_specs.pop(0)
            saved = self._tenv
            self._tenv = dict(bindings)
            try:
                self._emit_fn(bodies, fn, None, cname)
            finally:
                self._tenv = saved

        main_w = Writer()
        self._emit_c_main(main_w)

        out = Writer()
        out.parts.append(RUNTIME_C)
        out.line()
        out.line("/* ---- program types ---- */")
        for block in self.type_defs:
            out.parts.append(block)
            out.line()
        out.line("/* ---- constants ---- */")
        out.parts.extend(consts_w.parts)
        out.line()
        out.line("/* ---- prototypes ---- */")
        out.parts.extend(protos.parts)
        out.parts.extend(self.spec_protos)
        out.line()
        if self.lambda_defs:
            out.line("/* ---- closures ---- */")
            for block in self.lambda_defs:
                out.parts.append(block)
                out.line()
        out.parts.extend(init_w.parts)
        out.line()
        out.line("/* ---- program ---- */")
        out.parts.extend(bodies.parts)
        out.parts.extend(main_w.parts)
        return out.text()

    def _cname(self, fn: ast.FnDef, owner: str | None) -> str:
        if fn.generics and owner is None and self._tenv:
            key = (fn.name, tuple(sorted(self._tenv.items(), key=lambda kv: kv[0])))
            if key in self._specs:
                return self._specs[key]
        if owner is not None:
            return f"{owner}__{fn.name}"
        if fn.name == "main":
            return "axon_user_main"
        return fn.name

    def _prototype(self, fn: ast.FnDef, owner: str | None = None) -> str:
        ret_tag = self.type_tag(fn.return_type)
        self.ensure(ret_tag)
        ret = self.ctype(ret_tag)
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
            ptag = self.type_tag(p.type)
            self.ensure(ptag)
            params.append(f"{self.ctype(ptag)} {p.name}")
        param_str = ", ".join(params) if params else "void"
        return f"{ret} {self._cname(fn, owner)}({param_str})"

    def _emit_fn(self, w: Writer, fn: ast.FnDef, owner: str | None = None,
                 spec_name: str | None = None) -> None:
        if fn.generics and spec_name is None:
            raise unsupported(f"native backend does not support generic fn `{fn.name}`", fn.span)
        if fn.body is None:
            return
        ret_tag = self.type_tag(fn.return_type)
        ctx = FnContext(ret_tag=ret_tag)
        rest = fn.params
        if rest and rest[0].is_self:
            ctx.self_type = owner
            ctx.self_ptr = rest[0].is_mut
            rest = rest[1:]
        for p in rest:
            ctx.var_types[p.name] = self.type_tag(p.type)
            if p.is_mut:
                ctx.mut_vars.add(p.name)
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
        w.line("ax_argc = argc; ax_argv = argv;")
        w.line("ax_init_consts();")
        if ret_tag == "int":
            w.line("return (int)axon_user_main();")
        else:
            w.line("axon_user_main();")
            w.line("return 0;")
        w.dedent()
        w.line("}")

    # -- statements -----------------------------------------------------------
    def _emit_block(self, w: Writer, block: ast.Block, ctx: FnContext) -> None:
        ctx.defer_scopes.append([])
        for stmt in block.stmts:
            self._emit_stmt(w, stmt, ctx)
        scope = ctx.defer_scopes.pop()
        ends = block.stmts and isinstance(
            block.stmts[-1], (ast.ReturnStmt, ast.BreakStmt, ast.ContinueStmt))
        if scope and not ends:
            for expr in reversed(scope):
                self._emit_expr_stmt(w, expr, ctx)

    def _emit_loop_body(self, w: Writer, block: ast.Block, ctx: FnContext) -> None:
        ctx.loop_marks.append(len(ctx.defer_scopes))
        self._emit_block(w, block, ctx)
        ctx.loop_marks.pop()

    def _emit_defers(self, w: Writer, ctx: FnContext, start: int = 0) -> None:
        """Run the deferred expressions of every open scope from `start`
        inward, innermost first (the order the interpreter unwinds them)."""
        for scope in reversed(ctx.defer_scopes[start:]):
            for expr in reversed(scope):
                self._emit_expr_stmt(w, expr, ctx)

    def _emit_return(self, w: Writer, code: str | None, ctx: FnContext, tag=None) -> None:
        if not ctx.has_defers():
            w.line(f"return {code};" if code is not None else "return;")
            return
        w.line("{")
        w.indent()
        if code is not None:
            ret = ctx.tmp()
            rtag = tag if tag is not None else ctx.ret_tag
            w.line(f"{self.ctype(rtag)} {ret} = {code};")
        self._emit_defers(w, ctx)
        w.line(f"return {ret};" if code is not None else "return;")
        w.dedent()
        w.line("}")

    def _bind_pattern(self, w: Writer, pat: ast.Node, scr: str, tag, ctx: FnContext,
                      what: str, mutable: bool = False) -> None:
        """Destructure `scr` with an (expected irrefutable) pattern."""
        test, bindings = self._pattern_test(pat, scr, tag, ctx)
        if test != "true":
            w.line(f'if (!({test})) ax_panic(ax_str_lit("{what} pattern did not match"));')
        for name, btag, bcode in bindings:
            self.ensure(btag)
            ctx.var_types[name] = btag
            if mutable:
                ctx.mut_vars.add(name)
            w.line(f"{self.ctype(btag)} {name} = {bcode};")

    def _emit_stmt(self, w: Writer, stmt: ast.Node, ctx: FnContext) -> None:
        k = type(stmt)
        if k is ast.LetStmt:
            self._emit_let(w, stmt, ctx)
        elif k is ast.AssignStmt:
            self._emit_assign(w, stmt, ctx)
        elif k is ast.ReturnStmt:
            if stmt.value is None:
                self._emit_return(w, None, ctx)
            else:
                code, tag = self._emit_expr(stmt.value, ctx, w, expect=ctx.ret_tag)
                self._emit_return(w, code, ctx, ctx.ret_tag if ctx.ret_tag is not None else tag)
        elif k is ast.DeferStmt:
            if not ctx.defer_scopes:
                raise unsupported("`defer` outside of a block", stmt.span)
            ctx.defer_scopes[-1].append(stmt.expr)
        elif k is ast.ExprStmt:
            self._emit_expr_stmt(w, stmt.expr, ctx)
        elif k is ast.IfStmt:
            self._emit_if(w, stmt, ctx)
        elif k is ast.IfLetStmt:
            self._emit_if_let(w, stmt, ctx)
        elif k is ast.ForStmt:
            self._emit_for(w, stmt, ctx)
        elif k is ast.WhileStmt:
            # condition may need prelude statements; re-evaluate inside the loop
            pre = Writer()
            cond, ctag = self._emit_expr(stmt.cond, ctx, pre)
            if pre.parts:
                w.line("while (1) {")
                w.indent()
                for p in pre.parts:
                    w.line(p)
                w.line(f"if (!({self._truthy(cond, ctag)})) break;")
                self._emit_loop_body(w, stmt.body, ctx)
                w.dedent()
                w.line("}")
            else:
                w.line(f"while ({self._truthy(cond, ctag)}) {{")
                w.indent(); self._emit_loop_body(w, stmt.body, ctx); w.dedent()
                w.line("}")
        elif k is ast.LoopStmt:
            w.line("while (1) {")
            w.indent(); self._emit_loop_body(w, stmt.body, ctx); w.dedent()
            w.line("}")
        elif k is ast.BreakStmt or k is ast.ContinueStmt:
            if ctx.loop_marks and ctx.has_defers(ctx.loop_marks[-1]):
                self._emit_defers(w, ctx, ctx.loop_marks[-1])
            w.line("break;" if k is ast.BreakStmt else "continue;")
        else:
            raise unsupported(
                f"native backend does not support `{k.__name__}` yet",
                getattr(stmt, "span", None),
            )

    def _sequence(self, items: list, ctx: FnContext, w: Writer) -> list[str]:
        """Fix the evaluation order of sibling operands.

        C leaves the order of function arguments and operator operands
        unspecified, but AXON evaluates left to right. When an operand other
        than the last does real work (contains a call), every non-trivial
        operand except the last is bound to a temporary first. `items` holds
        (code, tag) pairs; the returned codes are safe to combine.
        """
        if not any("(" in c and not _LITERAL_RE.match(c) for c, _ in items[:-1]):
            return [c for c, _ in items]  # nothing before the last operand does work
        out = []
        for i, (code, tag) in enumerate(items):
            if i == len(items) - 1 or _LITERAL_RE.match(code) or tag == "void" \
                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", code):
                out.append(code)
                continue
            tmp = ctx.tmp()
            self.ensure(tag)
            w.line(f"{self.ctype(tag)} {tmp} = {code};")
            out.append(tmp)
        return out

    def _truthy(self, code: str, tag) -> str:
        if tag == "bool":
            return code
        if tag == "int":
            return f"({code} != 0)"
        if tag == "str":
            return f"({code}.len != 0)"
        if isinstance(tag, tuple) and tag[0] == "opt":
            return f"({code}.has)"
        if isinstance(tag, tuple) and tag[0] == "res":
            return f"({code}.ok)"
        return code

    def _emit_let(self, w: Writer, stmt: ast.LetStmt, ctx: FnContext) -> None:
        if stmt.catch_block is not None:
            self._emit_let_catch(w, stmt, ctx)
            return
        if stmt.value is None:
            tag = self.type_tag(stmt.type) if stmt.type is not None else "int"
            self.ensure(tag)
            ctx.var_types[stmt.name] = tag
            ctx.mut_vars.add(stmt.name)  # initialized later by assignment
            w.line(f"{self.ctype(tag)} {stmt.name};")
            w.line(f"memset(&{stmt.name}, 0, sizeof({stmt.name}));")
            return
        if stmt.pattern is not None:
            code, tag = self._emit_expr(stmt.value, ctx, w)
            self.ensure(tag)
            tmp = ctx.tmp()
            w.line(f"{self.ctype(tag)} {tmp} = {code};")
            self._bind_pattern(w, stmt.pattern, tmp, tag, ctx, "let", stmt.is_mut)
            return
        if stmt.is_mut:
            ctx.mut_vars.add(stmt.name)
        expect = self.type_tag(stmt.type) if stmt.type is not None else None
        code, tag = self._emit_expr(stmt.value, ctx, w, expect=expect)
        if expect is not None:
            tag = expect
        if tag == "void":
            raise unsupported(
                f"cannot infer a native type for `{stmt.name}`", stmt.span,
            )
        self.ensure(tag)
        ctx.var_types[stmt.name] = tag
        w.line(f"{self.ctype(tag)} {stmt.name} = {code};")

    def _emit_let_catch(self, w: Writer, stmt: ast.LetStmt, ctx: FnContext) -> None:
        # `let x = try EXPR catch e then <block>`
        inner = stmt.value.expr if isinstance(stmt.value, ast.TryExpr) else stmt.value
        code, tag = self._emit_expr(inner, ctx, w)
        if not (isinstance(tag, tuple) and tag[0] in {"res", "opt"}):
            raise unsupported("`catch` requires an Option or Result value", stmt.span)
        tmp = ctx.tmp()
        w.line(f"{self.ctype(tag)} {tmp} = {code};")
        if tag[0] == "res":
            ok_test, val_code, err_tag, err_code = (
                f"{tmp}.ok", f"{tmp}.val", tag[2], f"{tmp}.err")
            val_tag = tag[1]
        else:
            ok_test, val_code, err_tag, err_code = (
                f"{tmp}.has", f"{tmp}.val", tag, tmp)
            val_tag = tag[1]
        w.line(f"if (!{ok_test}) {{")
        w.indent()
        if stmt.catch_name:
            self.ensure(err_tag)
            ctx.var_types[stmt.catch_name] = err_tag
            w.line(f"{self.ctype(err_tag)} {stmt.catch_name} = {err_code};")
        self._emit_block(w, stmt.catch_block, ctx)
        w.dedent()
        w.line("}")
        ctx.var_types[stmt.name] = val_tag
        self.ensure(val_tag)
        w.line(f"{self.ctype(val_tag)} {stmt.name} = {val_code};")

    def _emit_assign(self, w: Writer, stmt: ast.AssignStmt, ctx: FnContext) -> None:
        target = stmt.target
        if isinstance(target, ast.IndexExpr):
            self._emit_index_assign(w, stmt, ctx)
            return
        tcode, ttag = self._emit_expr(target, ctx, w)
        value, vtag = self._emit_expr(stmt.value, ctx, w, expect=ttag)
        if stmt.op == "=":
            w.line(f"{tcode} = {value};")
            return
        op = stmt.op[:-1]
        if op == "+" and ttag == "str":
            w.line(f"{tcode} = ax_str_concat({tcode}, {value});")
        elif ttag in {"int", "float"}:
            w.line(f"{tcode} = {self._arith(op, tcode, value, ttag)};")
        else:
            w.line(f"{tcode} = {tcode} {op} {value};")

    def _emit_index_assign(self, w: Writer, stmt: ast.AssignStmt, ctx: FnContext) -> None:
        target = stmt.target
        recv, rtag = self._emit_expr(target.recv, ctx, w)
        if isinstance(rtag, tuple) and rtag[0] == "vec":
            s = mangle(rtag[1])
            idx, _ = self._emit_expr(target.index, ctx, w)
            if stmt.op == "=":
                value, _ = self._emit_expr(stmt.value, ctx, w, expect=rtag[1])
                w.line(f"ax_vec_set_{s}(&({recv}), {idx}, {value});")
            else:
                tmp = ctx.tmp()
                w.line(f"int64_t {tmp} = ax_vec_idx_{s}({recv}, {idx});")
                cur = f"({recv}).data[{tmp}]"
                value, _ = self._emit_expr(stmt.value, ctx, w, expect=rtag[1])
                op = stmt.op[:-1]
                if op == "+" and rtag[1] == "str":
                    w.line(f"{cur} = ax_str_concat({cur}, {value});")
                else:
                    w.line(f"{cur} = {self._arith(op, cur, value, rtag[1])};")
            return
        if isinstance(rtag, tuple) and rtag[0] == "map":
            s = mangle(rtag[1])
            key, _ = self._emit_expr(target.index, ctx, w, expect="str")
            if stmt.op == "=":
                value, _ = self._emit_expr(stmt.value, ctx, w, expect=rtag[1])
                w.line(f"ax_map_insert_{s}(&({recv}), {key}, {value});")
            else:
                op = stmt.op[:-1]
                cur = f"ax_map_index_{s}({recv}, {key})"
                value, _ = self._emit_expr(stmt.value, ctx, w, expect=rtag[1])
                if op == "+" and rtag[1] == "str":
                    new = f"ax_str_concat({cur}, {value})"
                else:
                    new = self._arith(op, cur, value, rtag[1])
                w.line(f"ax_map_insert_{s}(&({recv}), {key}, {new});")
            return
        raise unsupported("invalid assignment target for the native backend", stmt.span)

    def _emit_if(self, w: Writer, stmt: ast.IfStmt, ctx: FnContext) -> None:
        cond, ctag = self._emit_expr(stmt.cond, ctx, w)
        w.line(f"if ({self._truthy(cond, ctag)}) {{")
        w.indent(); self._emit_block(w, stmt.then, ctx); w.dedent()
        closes = 0
        for i, (econd, eblock) in enumerate(stmt.elifs):
            pre = Writer()
            ec, etag = self._emit_expr(econd, ctx, pre)
            if pre.parts:
                # the condition needs prelude statements; preserve evaluation
                # order by nesting inside the else branch
                w.line("} else {")
                w.indent()
                for p in pre.parts:
                    w.line(p)
                w.line(f"if ({self._truthy(ec, etag)}) {{")
                w.indent(); self._emit_block(w, eblock, ctx); w.dedent()
                closes += 1
            else:
                w.line(f"}} else if ({self._truthy(ec, etag)}) {{")
                w.indent(); self._emit_block(w, eblock, ctx); w.dedent()
        if stmt.els is not None:
            w.line("} else {")
            w.indent(); self._emit_block(w, stmt.els, ctx); w.dedent()
        w.line("}")
        for _ in range(closes):
            w.dedent()
            w.line("}")

    def _emit_if_let(self, w: Writer, stmt: ast.IfLetStmt, ctx: FnContext) -> None:
        code, tag = self._emit_expr(stmt.expr, ctx, w)
        tmp = ctx.tmp()
        self.ensure(tag)
        w.line(f"{self.ctype(tag)} {tmp} = {code};")
        test, bindings = self._pattern_test(stmt.pattern, tmp, tag, ctx)
        w.line(f"if ({test}) {{")
        w.indent()
        for name, btag, bcode in bindings:
            self.ensure(btag)
            ctx.var_types[name] = btag
            w.line(f"{self.ctype(btag)} {name} = {bcode};")
        self._emit_block(w, stmt.then, ctx)
        w.dedent()
        if stmt.els is not None:
            w.line("} else {")
            w.indent(); self._emit_block(w, stmt.els, ctx); w.dedent()
        w.line("}")

    def _emit_for(self, w: Writer, stmt: ast.ForStmt, ctx: FnContext) -> None:
        if isinstance(stmt.iterable, ast.RangeExpr):
            rng = stmt.iterable
            if isinstance(stmt.pattern, ast.BindingPattern):
                var = stmt.pattern.name
            else:
                var = ctx.tmp()
            start, _ = self._emit_expr(rng.start, ctx, w) if rng.start is not None else ("0", "int")
            end, _ = self._emit_expr(rng.end, ctx, w) if rng.end is not None else (start, "int")
            end_tmp = ctx.tmp()
            w.line(f"int64_t {end_tmp} = {end};")
            cmp = "<=" if rng.inclusive else "<"
            ctx.var_types[var] = "int"
            w.line(f"for (int64_t {var} = {start}; {var} {cmp} {end_tmp}; {var}++) {{")
            w.indent(); self._emit_loop_body(w, stmt.body, ctx); w.dedent()
            w.line("}")
            return
        code, tag = self._emit_expr(stmt.iterable, ctx, w)
        if tag == "str":
            self.ensure(("vec", "str"))
            code, tag = f"ax_str_chars({code})", ("vec", "str")
        if isinstance(tag, tuple) and tag[0] in {"vec", "map"}:
            coll, idx = ctx.tmp(), ctx.tmp()
            self.ensure(tag)
            w.line(f"{self.ctype(tag)} {coll} = {code};")
            if tag[0] == "vec":
                elem_tag = tag[1]
                w.line(f"for (int64_t {idx} = 0; {idx} < {coll}.len; {idx}++) {{")
                w.indent()
                elem = f"{coll}.data[{idx}]"
            else:
                elem_tag = ("tup", "str", tag[1])
                self.ensure(elem_tag)
                w.line(f"for (int64_t {idx} = 0; {idx} < {coll}.keys.len; {idx}++) {{")
                w.indent()
                elem = ctx.tmp()
                w.line(f"{self.ctype(elem_tag)} {elem} = (({self.ctype(elem_tag)})"
                       f"{{ {coll}.keys.data[{idx}], {coll}.vals.data[{idx}] }});")
            self._bind_pattern(w, stmt.pattern, elem, elem_tag, ctx, "for")
            self._emit_loop_body(w, stmt.body, ctx)
            w.dedent()
            w.line("}")
            return
        raise unsupported(
            "native `for` supports ranges, Vec, Map, and str values", stmt.span,
        )

    def _emit_expr_stmt(self, w: Writer, expr: ast.Node, ctx: FnContext) -> None:
        if isinstance(expr, ast.MatchExpr):
            self._emit_match(w, expr, ctx, want_value=False)
            return
        if isinstance(expr, ast.CallExpr) and isinstance(expr.callee, ast.NameExpr):
            name = expr.callee.name
            if name in {"println", "print", "eprintln", "eprint"}:
                self._emit_io(w, name, expr.args, ctx)
                return
            if name in {"panic", "todo", "unreachable", "unimplemented"}:
                default = {
                    "panic": "explicit panic",
                    "todo": "not yet implemented",
                    "unreachable": "entered unreachable code",
                    "unimplemented": "not implemented",
                }[name]
                if expr.args:
                    msg = self._to_str(w, *self._emit_expr(expr.args[0].value, ctx, w))
                else:
                    msg = f'ax_str_lit("{default}")'
                w.line(f"ax_panic({msg});")
                return
            if name == "assert":
                cond, ctag = self._emit_expr(expr.args[0].value, ctx, w)
                msg = (self._to_str(w, *self._emit_expr(expr.args[1].value, ctx, w))
                       if len(expr.args) > 1 else 'ax_str_lit("assertion failed")')
                w.line(f"ax_assert({self._truthy(cond, ctag)}, {msg});")
                return
            if name == "assert_eq":
                a, atag = self._emit_expr(expr.args[0].value, ctx, w)
                b, btag = self._emit_expr(expr.args[1].value, ctx, w, expect=atag)
                eq = self._eq_fn(atag)
                if eq is None:
                    raise unsupported(
                        f"assert_eq is not supported for `{atag}` natively", expr.span)
                ta, tb = ctx.tmp(), ctx.tmp()
                w.line(f"{self.ctype(atag)} {ta} = {a};")
                w.line(f"{self.ctype(atag)} {tb} = {b};")
                show = self.ensure_show(atag) if atag != "str" else None
                sa = f"{show}({ta})" if show else ta
                sb = f"{show}({tb})" if show else tb
                msg = (f'ax_str_concat(ax_str_concat(ax_str_concat('
                       f'ax_str_lit("assert_eq failed: "), {sa}), '
                       f'ax_str_lit(" != ")), {sb})')
                if len(expr.args) > 2:
                    msg = self._to_str(w, *self._emit_expr(expr.args[2].value, ctx, w))
                w.line(f"ax_assert({eq(ta, tb)}, {msg});")
                return
        code, _ = self._emit_expr(expr, ctx, w)
        w.line(f"{code};")

    def _emit_io(self, w: Writer, name: str, args: list[ast.Arg], ctx: FnContext) -> None:
        fn = {"println": "ax_println", "print": "ax_print",
              "eprintln": "ax_eprintln", "eprint": "ax_eprint"}[name]
        if not args:
            w.line(f'{fn}(ax_str_lit(""));')
            return
        pieces = self._sequence(
            [(self._to_str(w, *self._emit_expr(a.value, ctx, w)), "str") for a in args], ctx, w)
        acc = pieces[0]
        for p in pieces[1:]:
            acc = f'ax_str_concat(ax_str_concat({acc}, ax_str_lit(" ")), {p})'
        w.line(f"{fn}({acc});")

    # -- match ------------------------------------------------------------------
    def _emit_match(self, w: Writer, node: ast.MatchExpr, ctx: FnContext,
                    want_value: bool):
        scr, stag = self._emit_expr(node.scrutinee, ctx, w)
        tmp = ctx.tmp()
        self.ensure(stag)
        w.line(f"{self.ctype(stag)} {tmp} = {scr};")
        done = ctx.tmp()
        w.line(f"bool {done} = false;")

        result_var = None
        result_tag = None
        if want_value:
            result_tag = self._match_value_tag(node, tmp, stag, ctx)
            self.ensure(result_tag)
            result_var = ctx.tmp()
            w.line(f"{self.ctype(result_tag)} {result_var};")
            w.line(f"memset(&{result_var}, 0, sizeof({result_var}));")

        for arm in node.arms:
            test, bindings = self._pattern_test(arm.pattern, tmp, stag, ctx)
            w.line(f"if (!{done} && ({test})) {{")
            w.indent()
            for name, btag, bcode in bindings:
                self.ensure(btag)
                ctx.var_types[name] = btag
                w.line(f"{self.ctype(btag)} {name} = {bcode};")
            body_writer = w
            if arm.guard is not None:
                gcode, gtag = self._emit_expr(arm.guard, ctx, w)
                w.line(f"if ({self._truthy(gcode, gtag)}) {{")
                w.indent()
            w.line(f"{done} = true;")
            self._emit_arm_body(body_writer, arm.body, ctx, result_var, result_tag)
            if arm.guard is not None:
                w.dedent()
                w.line("}")
            w.dedent()
            w.line("}")
        w.line(f'if (!{done}) ax_panic(ax_str_lit("non-exhaustive match"));')
        if want_value:
            return result_var, result_tag
        return None

    def _emit_arm_body(self, w: Writer, body: ast.Block, ctx: FnContext,
                       result_var, result_tag) -> None:
        stmts = body.stmts
        ctx.defer_scopes.append([])
        for i, stmt in enumerate(stmts):
            last = i == len(stmts) - 1
            if last and result_var is not None and isinstance(stmt, ast.ExprStmt):
                code, _ = self._emit_expr(stmt.expr, ctx, w, expect=result_tag)
                w.line(f"{result_var} = {code};")
            else:
                self._emit_stmt(w, stmt, ctx)
        for expr in reversed(ctx.defer_scopes.pop()):
            self._emit_expr_stmt(w, expr, ctx)

    def _match_value_tag(self, node: ast.MatchExpr, scr: str, stag,
                         ctx: FnContext):
        """Infer the value type of a match expression from its first value arm
        by generating it into a throwaway writer."""
        for arm in node.arms:
            if arm.body.stmts and isinstance(arm.body.stmts[-1], ast.ExprStmt):
                scratch = Writer()
                probe = FnContext(ctx.self_type, ctx.self_ptr, ctx.ret_tag)
                probe.var_types = dict(ctx.var_types)
                _, bindings = self._pattern_test(arm.pattern, scr, stag, probe)
                for name, btag, _ in bindings:
                    probe.var_types[name] = btag
                _, tag = self._emit_expr(arm.body.stmts[-1].expr, probe, scratch)
                return tag
        raise unsupported("match expression has no value arms", node.span)

    # -- pattern compilation ------------------------------------------------------
    def _pattern_test(self, pat: ast.Node, scr: str, tag, ctx: FnContext):
        """Compile a pattern into (C test expression, [(name, tag, access)])."""
        k = type(pat)
        if k is ast.WildcardPattern:
            return "true", []
        if k is ast.BindingPattern:
            return "true", [(pat.name, tag, scr)]
        if k is ast.LiteralPattern:
            code, ltag = self._emit_expr(pat.value, ctx, Writer())
            eq = self._eq_fn(tag if tag != "void" else ltag)
            if eq is None:
                raise unsupported("native match cannot compare this pattern",
                                  pat.span)
            return eq(scr, code), []
        if k is ast.OrPattern:
            tests = []
            for alt in pat.alts:
                t, binds = self._pattern_test(alt, scr, tag, ctx)
                if binds:
                    raise unsupported(
                        "bindings inside or-patterns are not supported natively",
                        pat.span,
                    )
                tests.append(f"({t})")
            return " || ".join(tests), []
        if k is ast.VariantPattern:
            return self._variant_test(pat, scr, tag, ctx)
        if k is ast.TuplePattern:
            if not (isinstance(tag, tuple) and tag[0] == "tup"
                    and len(tag) - 1 == len(pat.elems)):
                raise unsupported(f"tuple pattern cannot match `{tag}` natively", pat.span)
            tests: list[str] = []
            binds: list = []
            for i, sub in enumerate(pat.elems):
                t, b = self._pattern_test(sub, f"{scr}.f{i}", tag[i + 1], ctx)
                if t != "true":
                    tests.append(f"({t})")
                binds.extend(b)
            return (" && ".join(tests) if tests else "true"), binds
        raise unsupported(
            f"native match does not support `{k.__name__}` patterns",
            getattr(pat, "span", None),
        )

    def _variant_test(self, pat: ast.VariantPattern, scr: str, tag, ctx: FnContext):
        name = pat.name
        if isinstance(tag, tuple) and tag[0] == "opt":
            if name == "Some":
                if not pat.subpatterns:
                    return f"{scr}.has", []
                sub_test, binds = self._pattern_test(
                    pat.subpatterns[0], f"{scr}.val", tag[1], ctx)
                return f"{scr}.has && ({sub_test})", binds
            if name == "None":
                return f"!{scr}.has", []
        if isinstance(tag, tuple) and tag[0] == "res":
            if name == "Ok":
                if not pat.subpatterns:
                    return f"{scr}.ok", []
                sub_test, binds = self._pattern_test(
                    pat.subpatterns[0], f"{scr}.val", tag[1], ctx)
                return f"{scr}.ok && ({sub_test})", binds
            if name == "Err":
                if not pat.subpatterns:
                    return f"!{scr}.ok", []
                sub_test, binds = self._pattern_test(
                    pat.subpatterns[0], f"{scr}.err", tag[2], ctx)
                return f"!{scr}.ok && ({sub_test})", binds
        if isinstance(tag, tuple) and tag[0] == "enum":
            edef = self.enums[tag[1]]
            vdef = next((v for v in edef.variants if v.name == name), None)
            if vdef is None:
                raise unsupported(
                    f"`{tag[1]}` has no variant `{name}`", pat.span)
            has_payload = self._enum_has_payload(edef)
            tag_access = f"{scr}.tag" if has_payload else scr
            test = f"{tag_access} == {tag[1]}__{name}"
            binds: list = []
            tests = [test]
            for i, sub in enumerate(pat.subpatterns):
                # resolve payload slot: named pattern fields map by name
                slot = i
                if i < len(pat.field_names) and pat.field_names[i] is not None:
                    for j, (pname, _) in enumerate(vdef.payload):
                        if pname == pat.field_names[i]:
                            slot = j
                            break
                if slot >= len(vdef.payload):
                    raise unsupported(
                        f"variant `{name}` has no payload slot {slot}", pat.span)
                pname, ptype = vdef.payload[slot]
                cname = cfield(pname) if pname else f"f{slot}"
                ptag = self.type_tag(ptype)
                sub_test, sub_binds = self._pattern_test(
                    sub, f"{scr}.as_.{name}.{cname}", ptag, ctx)
                if sub_test != "true":
                    tests.append(f"({sub_test})")
                binds.extend(sub_binds)
            return " && ".join(tests), binds
        raise unsupported(
            f"native match cannot test variant `{name}` against `{tag}`",
            pat.span,
        )

    # -- expressions ----------------------------------------------------------
    def _emit_expr(self, node: ast.Node, ctx: FnContext, w: Writer,
                   expect=None) -> tuple[str, object]:
        k = type(node)
        if k is ast.IntLit:
            if expect == "float":
                return f"{node.value}.0", "float"
            return f"INT64_C({node.value})" if abs(node.value) > 2**31 else str(node.value), "int"
        if k is ast.FloatLit:
            text = repr(node.value)
            if "." not in text and "e" not in text and "inf" not in text and "nan" not in text:
                text += ".0"
            return text, "float"
        if k is ast.BoolLit:
            return ("true" if node.value else "false"), "bool"
        if k is ast.StrLit:
            return f"ax_str_lit({self._c_string(node.value)})", "str"
        if k is ast.CharLit:
            return f"ax_str_lit({self._c_string(node.value)})", "str"
        if k is ast.NoneLit:
            if isinstance(expect, tuple) and expect[0] == "opt":
                self.ensure(expect)
                return f"ax_opt_none_{mangle(expect[1])}()", expect
            raise unsupported(
                "`none` needs a known Option type in native code", node.span)
        if k is ast.FStr:
            return self._emit_fstr(node, ctx, w), "str"
        if k is ast.NameExpr:
            return self._emit_name(node, ctx, expect)
        if k is ast.SelfExpr:
            return ("(*self)" if ctx.self_ptr else "self"), (ctx.self_type or "void")
        if k is ast.BinaryExpr:
            return self._emit_binary(node, ctx, w)
        if k is ast.UnaryExpr:
            return self._emit_unary(node, ctx, w)
        if k is ast.RefExpr:
            return self._emit_expr(node.operand, ctx, w, expect)
        if k is ast.CallExpr:
            return self._emit_call(node, ctx, w, expect)
        if k is ast.FieldExpr:
            return self._emit_field(node, ctx, w)
        if k is ast.IndexExpr:
            return self._emit_index(node, ctx, w)
        if k is ast.RecordLit:
            return self._emit_record(node, ctx, w)
        if k is ast.ListLit:
            return self._emit_list(node, ctx, w, expect)
        if k is ast.ArrayRepeat:
            return self._emit_array_repeat(node, ctx, w, expect)
        if k is ast.IfExpr:
            return self._emit_if_expr(node, ctx, w, expect)
        if k is ast.TupleLit:
            return self._emit_tuple(node, ctx, w, expect)
        if k is ast.LambdaExpr:
            return self._emit_lambda(node, ctx, w, expect)
        if k is ast.PipeExpr:
            return self._emit_expr(self._desugar_pipe(node), ctx, w, expect)
        if k is ast.OptFieldExpr:
            return self._emit_opt_chain(node, None, ctx, w)
        if k is ast.MatchExpr:
            result_var, result_tag = self._emit_match(w, node, ctx, want_value=True)
            return result_var, result_tag
        if k is ast.TryExpr:
            return self._emit_try(node, ctx, w)
        if k is ast.CastExpr:
            return self._emit_cast(node, ctx, w)
        raise unsupported(
            f"native backend does not support expression `{k.__name__}`",
            getattr(node, "span", None),
        )

    def _emit_name(self, node: ast.NameExpr, ctx: FnContext, expect=None):
        name = node.name
        if name in ctx.var_types:
            return name, ctx.var_types[name]
        if name in self.const_tags:
            return name, self.const_tags[name]
        if name in self.consts:
            # const referenced before its tag is computed (const-init order)
            raise unsupported(
                f"constant `{name}` must be declared before use", node.span)
        if name in self.enum_variant_owner:
            owner = self.enum_variant_owner[name]
            edef = self.enums[owner]
            vdef = next(v for v in edef.variants if v.name == name)
            if vdef.payload:
                raise unsupported(
                    f"variant `{name}` needs payload arguments", node.span)
            tag = ("enum", owner)
            self.ensure(tag)
            if self._enum_has_payload(edef):
                return f"{owner}_mk_{name}()", tag
            return f"{owner}__{name}", tag
        if name in self.functions:
            return self._emit_fn_ref(self.functions[name], node.span)
        raise unsupported(
            f"native backend cannot resolve `{name}`",
            node.span,
        )

    def _emit_fn_ref(self, fn: ast.FnDef, span):
        """A named function used as a value: wrap it in a closure trampoline."""
        if fn.generics:
            raise unsupported(f"generic fn `{fn.name}` cannot be used as a value natively", span)
        ptags = []
        for p in fn.params:
            if p.type is None:
                raise unsupported(f"parameter `{p.name}` needs a type annotation", p.span)
            ptags.append(self.type_tag(p.type))
        ret = self.type_tag(fn.return_type)
        tag = ("fn", ret, *ptags)
        self.ensure(tag)
        tramp = f"__ax_fnref_{fn.name}"
        if fn.name not in self._fnrefs:
            self._fnrefs.add(fn.name)
            params = ", ".join(["void *env", *[f"{self.ctype(t)} a{i}" for i, t in enumerate(ptags)]])
            call = f"{self._cname(fn, None)}({', '.join(f'a{i}' for i in range(len(ptags)))})"
            body = f"{call};" if ret == "void" else f"return {call};"
            self.lambda_defs.append(
                f"static {self.ctype(ret)} {tramp}({params}) {{ (void)env; {body} }}")
        return f"(({self.ctype(tag)}){{ NULL, {tramp} }})", tag

    def _emit_binary(self, node: ast.BinaryExpr, ctx: FnContext, w: Writer):
        op = node.op
        # `x == none` / `x != none`
        if op in {"==", "!="} and isinstance(node.right, ast.NoneLit):
            lcode, ltag = self._emit_expr(node.left, ctx, w)
            if isinstance(ltag, tuple) and ltag[0] == "opt":
                return (f"(!{lcode}.has)" if op == "==" else f"({lcode}.has)"), "bool"
        if op in {"and", "or"}:
            lcode, ltag = self._emit_expr(node.left, ctx, w)
            pre = Writer()
            pre.level = w.level + 1
            rcode, rtag = self._emit_expr(node.right, ctx, pre)
            cop = "&&" if op == "and" else "||"
            if not pre.parts:
                return f"({self._truthy(lcode, ltag)} {cop} {self._truthy(rcode, rtag)})", "bool"
            # the right operand needs statements: evaluate it only when needed
            out = ctx.tmp()
            w.line(f"bool {out} = {self._truthy(lcode, ltag)};")
            w.line(f"if ({'' if op == 'and' else '!'}{out}) {{")
            w.parts.extend(pre.parts)
            w.line(f"    {out} = {self._truthy(rcode, rtag)};")
            w.line("}")
            return out, "bool"
        lcode, ltag = self._emit_expr(node.left, ctx, w)
        rcode, rtag = self._emit_expr(node.right, ctx, w, expect=ltag)
        lcode, rcode = self._sequence([(lcode, ltag), (rcode, rtag)], ctx, w)
        if op in {"==", "!="} and (ltag == "str" or rtag == "str"):
            base = f"ax_str_eq({lcode}, {rcode})"
            return (base if op == "==" else f"(!{base})"), "bool"
        if op in {"<", "<=", ">", ">="} and ltag == "str":
            return f"(ax_str_cmp({lcode}, {rcode}) {op} 0)", "bool"
        if op in {"==", "!="} and isinstance(ltag, tuple):
            eq = self._eq_fn(ltag)
            if eq is None:
                raise unsupported(
                    f"`==` is not supported for `{ltag}` natively", node.span)
            base = eq(lcode, rcode)
            return (base if op == "==" else f"(!{base})"), "bool"
        if op == "+" and (ltag == "str" or rtag == "str"):
            return f"ax_str_concat({lcode}, {rcode})", "str"
        if op == "*" and ltag == "str" and rtag == "int":
            return f"ax_str_repeat({lcode}, {rcode})", "str"
        if op in {"==", "!=", "<", "<=", ">", ">="}:
            return f"({lcode} {op} {rcode})", "bool"
        if op in {"&", "|", "^"} and ltag == "bool" and rtag == "bool":
            return f"({lcode} {op} {rcode})", "bool"
        result = "float" if "float" in (ltag, rtag) else "int"
        return self._arith(op, lcode, rcode, result), result

    def _arith(self, op: str, a: str, b: str, tag) -> str:
        """C expression for an arithmetic/bitwise operator on int or float."""
        if tag == "float":
            if op == "%":
                return f"fmod({a}, {b})"
            return f"({a} {op} {b})"
        helper = {"+": "ax_iadd", "-": "ax_isub", "*": "ax_imul", "/": "ax_idiv",
                  "%": "ax_imod", "<<": "ax_ishl", ">>": "ax_ishr"}.get(op)
        if helper is not None:
            return f"{helper}({a}, {b})"
        return f"({a} {op} {b})"

    def _emit_unary(self, node: ast.UnaryExpr, ctx: FnContext, w: Writer):
        code, tag = self._emit_expr(node.operand, ctx, w)
        if node.op == "not":
            return f"(!{self._truthy(code, tag)})", "bool"
        if node.op == "*":
            return code, tag
        if node.op == "-" and tag == "int":
            return f"ax_ineg({code})", "int"
        return f"({node.op}{code})", tag

    def _emit_if_expr(self, node: ast.IfExpr, ctx: FnContext, w: Writer, expect):
        cond, ctag = self._emit_expr(node.cond, ctx, w)
        tw, ew = Writer(), Writer()
        tw.level = ew.level = w.level + 1
        then, t = self._emit_expr(node.then, ctx, tw, expect)
        if t == "void" and isinstance(node.els, ast.NoneLit):
            # statement form: `if c then do_something()`
            w.line(f"if ({self._truthy(cond, ctag)}) {{")
            w.parts.extend(tw.parts)
            w.line(f"    {then};")
            w.line("}")
            return "((void)0)", "void"
        els, et = self._emit_expr(node.els, ctx, ew, expect or t)
        if t == "int" and et == "float":
            t = "float"
        if not tw.parts and not ew.parts:
            return f"({self._truthy(cond, ctag)} ? {then} : {els})", t
        # a branch needs statements: only the taken branch may run them
        if t == "void":
            w.line(f"if ({self._truthy(cond, ctag)}) {{")
            w.parts.extend(tw.parts)
            w.line(f"    {then};")
            w.line("} else {")
            w.parts.extend(ew.parts)
            w.line(f"    {els};")
            w.line("}")
            return "((void)0)", "void"
        out = ctx.tmp()
        self.ensure(t)
        w.line(f"{self.ctype(t)} {out};")
        w.line(f"if ({self._truthy(cond, ctag)}) {{")
        w.parts.extend(tw.parts)
        w.line(f"    {out} = {then};")
        w.line("} else {")
        w.parts.extend(ew.parts)
        w.line(f"    {out} = {els};")
        w.line("}")
        return out, t

    def _emit_tuple(self, node: ast.TupleLit, ctx: FnContext, w: Writer, expect):
        if not node.elems:
            return "((void)0)", "void"
        exp = expect if (isinstance(expect, tuple) and expect[0] == "tup"
                         and len(expect) == len(node.elems) + 1) else None
        codes, tags = [], []
        for i, e in enumerate(node.elems):
            code, tag = self._emit_expr(e, ctx, w, exp[i + 1] if exp else None)
            if exp is not None and tag == "int" and exp[i + 1] == "float":
                tag = "float"
            codes.append(code)
            tags.append(tag)
        tag = ("tup", *tags)
        self.ensure(tag)
        codes = self._sequence(list(zip(codes, tags)), ctx, w)
        return f"(({self.ctype(tag)}){{ {', '.join(codes)} }})", tag

    def _desugar_pipe(self, node: ast.PipeExpr) -> ast.Node:
        """`x |> f(a)` is `f(x, a)`; `x |> f` is `f(x)`."""
        left = ast.Arg(node.left.span, None, node.left)
        right = node.right
        if isinstance(right, ast.CallExpr):
            return ast.CallExpr(right.span, right.callee, [left, *right.args])
        return ast.CallExpr(node.span, right, [left])

    def _emit_opt_chain(self, node: ast.OptFieldExpr, args, ctx: FnContext, w: Writer):
        """`a?.field` / `a?.method(args)`: none when `a` is none, otherwise the
        projection wrapped in Some."""
        rcode, rtag = self._emit_expr(node.recv, ctx, w)
        self.ensure(rtag)
        recv = ctx.tmp()
        w.line(f"{self.ctype(rtag)} {recv} = {rcode};")
        is_opt = isinstance(rtag, tuple) and rtag[0] == "opt"
        inner_tag = rtag[1] if is_opt else rtag
        inner = ctx.tmp()
        ctx.var_types[inner] = inner_tag
        body = Writer()
        body.level = w.level + 1
        access = ast.FieldExpr(node.span, ast.NameExpr(node.span, inner), node.name)
        if args is None:
            vcode, vtag = self._emit_field(access, ctx, body)
        else:
            vcode, vtag = self._emit_method_call(access, args, ctx, body, None, node.span)
        if vtag == "void":
            raise unsupported("`?.` on a method without a result is not supported natively",
                              node.span)
        otag = ("opt", vtag)
        self.ensure(otag)
        out = ctx.tmp()
        w.line(f"{self.ctype(otag)} {out} = ax_opt_none_{mangle(vtag)}();")
        w.line(f"if ({recv + '.has' if is_opt else 'true'}) {{")
        val = f"{recv}.val" if is_opt else recv
        w.line(f"    {self.ctype(inner_tag)} {inner} = {val};")
        w.parts.extend(body.parts)
        w.line(f"    {out} = ax_opt_some_{mangle(vtag)}({vcode});")
        w.line("}")
        return out, otag

    # -- closures ---------------------------------------------------------------
    def _free_names(self, node, out: set) -> None:
        """Every identifier referenced under `node` (an over-approximation of
        the closure's free variables; non-locals are filtered by the caller)."""
        if isinstance(node, ast.NameExpr):
            out.add(node.name)
            return
        if isinstance(node, ast.SelfExpr):
            out.add("self")
            return
        if isinstance(node, list):
            for x in node:
                self._free_names(x, out)
            return
        if isinstance(node, tuple):
            for x in node:
                self._free_names(x, out)
            return
        if isinstance(node, ast.Node):
            for f in node.__dataclass_fields__:
                if f != "span":
                    self._free_names(getattr(node, f), out)

    def _emit_lambda(self, node: ast.LambdaExpr, ctx: FnContext, w: Writer, expect):
        exp = expect if (isinstance(expect, tuple) and expect[0] == "fn"
                         and len(expect) - 2 == len(node.params)) else None
        ptags = []
        for i, p in enumerate(node.params):
            if p.type is not None:
                ptags.append(self.type_tag(p.type))
            elif exp is not None and exp[i + 2] is not None:
                ptags.append(exp[i + 2])
            else:
                raise unsupported(
                    f"closure parameter `{p.name}` needs a type annotation for native "
                    f"compilation (e.g. `fn({p.name}: int) = ...`)", p.span)
        declared = self.type_tag(node.ret) if node.ret is not None else (
            exp[1] if exp is not None and exp[1] is not None else None)
        params = {p.name for p in node.params}
        names: set = set()
        self._free_names(node.body, names)
        captures = sorted(n for n in names - params
                          if n in ctx.var_types and n != "self")
        uses_self = "self" in names and ctx.self_type is not None
        for n in captures:
            if n in ctx.mut_vars:
                raise unsupported(
                    f"closures that capture the mutable variable `{n}` are not yet "
                    "supported natively", node.span)
        self._lambda_count += 1
        lid = self._lambda_count
        fname, env_t = f"__ax_lambda{lid}", f"AxEnv{lid}"

        lctx = FnContext(self_type=ctx.self_type if uses_self else None,
                         self_ptr=False, ret_tag=declared)
        for n in captures:
            lctx.var_types[n] = ctx.var_types[n]
        for p, t in zip(node.params, ptags):
            lctx.var_types[p.name] = t
        body = Writer()
        body.level = 1
        if isinstance(node.body, ast.Block):
            raise unsupported("block-bodied closures are not supported natively", node.span)
        code, rtag = self._emit_expr(node.body, lctx, body, expect=declared)
        if declared is not None:
            rtag = declared
        if rtag == "void" and code == "((void)0)":
            code = None
        tag = ("fn", rtag, *ptags)
        self.ensure(tag)

        fields = [(n, ctx.var_types[n]) for n in captures]
        if uses_self:
            fields.append(("self", ctx.self_type))
        lines = []
        if fields:
            decls = " ".join(f"{self.ctype(t)} {n};" for n, t in fields)
            lines.append(f"typedef struct {{ {decls} }} {env_t};")
        plist = ", ".join(["void *__env", *[f"{self.ctype(t)} {p.name}"
                                           for p, t in zip(node.params, ptags)]])
        lines.append(f"static {self.ctype(rtag)} {fname}({plist}) {{")
        if fields:
            lines.append(f"    {env_t} *__e = ({env_t} *)__env;")
            for n, t in fields:
                lines.append(f"    {self.ctype(t)} {n} = __e->{n};")
        else:
            lines.append("    (void)__env;")
        lines.extend(body.parts)
        if code is not None:
            lines.append(f"    {code};" if rtag == "void" else f"    return {code};")
        lines.append("}")
        self.lambda_defs.append("\n".join(lines))

        if not fields:
            return f"(({self.ctype(tag)}){{ NULL, {fname} }})", tag
        env = ctx.tmp()
        w.line(f"{env_t} *{env} = ({env_t} *)ax_alloc(sizeof({env_t}));")
        for n, _ in fields:
            if n == "self":
                w.line(f"{env}->self = {'(*self)' if ctx.self_ptr else 'self'};")
            else:
                w.line(f"{env}->{n} = {n};")
        return f"(({self.ctype(tag)}){{ {env}, {fname} }})", tag

    def _call_closure(self, code: str, tag, args, ctx: FnContext, w: Writer, span):
        params = tag[2:]
        if len(args) != len(params):
            raise unsupported(f"closure expects {len(params)} arguments", span)
        fn = ctx.tmp()
        w.line(f"{self.ctype(tag)} {fn} = {code};")
        codes = self._sequence(
            [(self._emit_expr(a.value, ctx, w, expect=pt)[0], pt) for a, pt in zip(args, params)],
            ctx, w)
        return f"{fn}.call({', '.join([f'{fn}.env', *codes])})", tag[1]

    def _closure_arg(self, arg, ret, params, ctx: FnContext, w: Writer, span, what: str):
        """Emit a closure argument and bind it to a temporary: (name, fn tag)."""
        code, tag = self._emit_expr(arg.value, ctx, w, expect=("fn", ret, *params))
        if not (isinstance(tag, tuple) and tag[0] == "fn" and len(tag) - 2 == len(params)):
            raise unsupported(f"{what} expects a closure taking {len(params)} argument(s)", span)
        fn = ctx.tmp()
        w.line(f"{self.ctype(tag)} {fn} = {code};")
        return fn, tag

    @staticmethod
    def _invoke(fn: str, *args: str) -> str:
        return f"{fn}.call({', '.join([f'{fn}.env', *args])})"

    def _emit_try(self, node: ast.TryExpr, ctx: FnContext, w: Writer):
        code, tag = self._emit_expr(node.expr, ctx, w)
        if not (isinstance(tag, tuple) and tag[0] in {"res", "opt"}):
            return code, tag  # `try` on a plain value is a no-op
        tmp = ctx.tmp()
        self.ensure(tag)
        w.line(f"{self.ctype(tag)} {tmp} = {code};")
        is_res = tag[0] == "res"
        ok_test = f"{tmp}.ok" if is_res else f"{tmp}.has"
        val_tag = tag[1]
        if node.or_else is not None:
            out = ctx.tmp()
            self.ensure(val_tag)
            w.line(f"{self.ctype(val_tag)} {out};")
            w.line(f"if ({ok_test}) {{ {out} = {tmp}.val; }} else {{")
            w.indent()
            fallback, _ = self._emit_expr(node.or_else, ctx, w, expect=val_tag)
            w.line(f"{out} = {fallback};")
            w.dedent()
            w.line("}")
            return out, val_tag
        if node.catch_expr is not None:
            out = ctx.tmp()
            self.ensure(val_tag)
            w.line(f"{self.ctype(val_tag)} {out};")
            w.line(f"if ({ok_test}) {{ {out} = {tmp}.val; }} else {{")
            w.indent()
            if node.catch_name:
                err_tag = tag[2] if is_res else tag
                err_code = f"{tmp}.err" if is_res else tmp
                self.ensure(err_tag)
                ctx.var_types[node.catch_name] = err_tag
                w.line(f"{self.ctype(err_tag)} {node.catch_name} = {err_code};")
            fallback, _ = self._emit_expr(node.catch_expr, ctx, w, expect=val_tag)
            w.line(f"{out} = {fallback};")
            w.dedent()
            w.line("}")
            return out, val_tag
        # `?`-style propagation: the enclosing function must return a
        # compatible Option/Result.
        rtag = ctx.ret_tag
        if is_res:
            if not (isinstance(rtag, tuple) and rtag[0] == "res" and rtag[2] == tag[2]):
                raise unsupported(
                    "`try` propagation requires the function to return a "
                    "Result with a matching error type", node.span)
            self.ensure(rtag)
            w.line(f"if (!{ok_test}) {{")
            w.indent()
            self._emit_return(w, f"ax_res_err_{mangle(rtag)}({tmp}.err)", ctx, rtag)
            w.dedent()
            w.line("}")
        else:
            if not (isinstance(rtag, tuple) and rtag[0] == "opt"):
                raise unsupported(
                    "`try` propagation on an Option requires the function to "
                    "return an Option", node.span)
            self.ensure(rtag)
            w.line(f"if (!{ok_test}) {{")
            w.indent()
            self._emit_return(w, f"ax_opt_none_{mangle(rtag[1])}()", ctx, rtag)
            w.dedent()
            w.line("}")
        return f"{tmp}.val", val_tag

    def _emit_cast(self, node: ast.CastExpr, ctx: FnContext, w: Writer):
        code, tag = self._emit_expr(node.expr, ctx, w)
        target = node.type
        if isinstance(target, ast.TypeName):
            n = target.name
            if n == "str":
                return self._to_str(w, code, tag), "str"
            if n in _INT_TYPES:
                if tag == "str":
                    raise unsupported("cast str to int natively via `.parse(int)`", node.span)
                if tag == "float":
                    return f"ax_f2i({code})", "int"
                return f"((int64_t)({code}))", "int"
            if n in _FLOAT_TYPES:
                return f"((double)({code}))", "float"
            if n == "bool":
                return f"({self._truthy(code, tag)})", "bool"
        ttag = self.type_tag(target)
        return f"(({self.ctype(ttag)}){code})", ttag

    # -- calls ------------------------------------------------------------------
    def _order_args(self, fn: ast.FnDef, args: list[ast.Arg], ctx: FnContext,
                    w: Writer, span=None) -> list[str]:
        params = fn.params[1:] if (fn.params and fn.params[0].is_self) else fn.params
        by_name = {a.name: a for a in args if a.name is not None}
        positional = [a for a in args if a.name is None]
        out: list = []
        pi = 0
        for p in params:
            expect = self.type_tag(p.type) if p.type is not None else None
            if pi < len(positional):
                code, tag = self._emit_expr(positional[pi].value, ctx, w, expect=expect)
                pi += 1
            elif p.name in by_name:
                code, tag = self._emit_expr(by_name[p.name].value, ctx, w, expect=expect)
            elif p.default is not None:
                code, tag = self._emit_expr(p.default, ctx, w, expect=expect)
            else:
                raise unsupported(
                    f"missing argument `{p.name}` in call to `{fn.name}`", span)
            out.append((code, expect if expect is not None else tag))
        return self._sequence(out, ctx, w)

    def _emit_call(self, node: ast.CallExpr, ctx: FnContext, w: Writer, expect=None):
        callee = node.callee
        if isinstance(callee, ast.NameExpr) and callee.name in ctx.var_types:
            tag = ctx.var_types[callee.name]
            if isinstance(tag, tuple) and tag[0] == "fn":
                return self._call_closure(callee.name, tag, node.args, ctx, w, node.span)
        if isinstance(callee, ast.OptFieldExpr):
            return self._emit_opt_chain(callee, node.args, ctx, w)
        if isinstance(callee, ast.NameExpr):
            name = callee.name
            if name == "Some":
                code, tag = self._emit_expr(node.args[0].value, ctx, w,
                                            expect=expect[1] if isinstance(expect, tuple) and expect[0] == "opt" else None)
                otag = ("opt", tag)
                self.ensure(otag)
                return f"ax_opt_some_{mangle(tag)}({code})", otag
            if name == "Ok":
                ok_expect = expect[1] if isinstance(expect, tuple) and expect[0] == "res" else None
                err_tag = expect[2] if isinstance(expect, tuple) and expect[0] == "res" else "str"
                code, tag = self._emit_expr(node.args[0].value, ctx, w, expect=ok_expect)
                rtag = ("res", tag, err_tag)
                self.ensure(rtag)
                return f"ax_res_ok_{mangle(rtag)}({code})", rtag
            if name == "Err":
                if isinstance(expect, tuple) and expect[0] == "res":
                    rtag = expect
                elif isinstance(ctx.ret_tag, tuple) and ctx.ret_tag[0] == "res":
                    rtag = ctx.ret_tag
                else:
                    raise unsupported(
                        "`Err(...)` needs a known Result type in native code",
                        node.span)
                code, _ = self._emit_expr(node.args[0].value, ctx, w, expect=rtag[2])
                self.ensure(rtag)
                return f"ax_res_err_{mangle(rtag)}({code})", rtag
            if name in self.functions:
                fn = self.functions[name]
                if fn.generics:
                    return self._emit_generic_call(fn, node.args, ctx, w, node.span)
                args = self._order_args(fn, node.args, ctx, w, node.span)
                return f"{self._cname(fn, None)}({', '.join(args)})", self.type_tag(fn.return_type)
            if name == "read_line" and not node.args:
                tag = ("opt", "str")
                self.ensure(tag)
                line, result = ctx.tmp(), ctx.tmp()
                w.line(f"AxStr {line};")
                w.line(f"{self.ctype(tag)} {result} = ax_opt_none_str();")
                w.line(f"if (ax_read_line(&{line})) {result} = ax_opt_some_str({line});")
                return result, tag
            if name in {"max", "min"} and len(node.args) == 2:
                a, atag = self._emit_expr(node.args[0].value, ctx, w)
                b, _ = self._emit_expr(node.args[1].value, ctx, w, expect=atag)
                if atag == "int":
                    return f"ax_i{name}({a}, {b})", "int"
                if atag == "float":
                    return f"ax_f{name}_({a}, {b})", "float"
            if name == "abs" and len(node.args) == 1:
                a, atag = self._emit_expr(node.args[0].value, ctx, w)
                if atag == "int":
                    return f"ax_iabs({a})", "int"
                if atag == "float":
                    return f"fabs({a})", "float"
            if name == "len" and len(node.args) == 1:
                a, atag = self._emit_expr(node.args[0].value, ctx, w)
                if atag == "str":
                    return f"ax_str_len({a})", "int"
                if isinstance(atag, tuple) and atag[0] == "vec":
                    return f"({a}).len", "int"
                if isinstance(atag, tuple) and atag[0] == "map":
                    return f"({a}).keys.len", "int"
            if name in self.enum_variant_owner:
                owner = self.enum_variant_owner[name]
                return self._emit_variant_ctor(owner, name, node.args, ctx, w, node.span)
            raise unsupported(f"native backend cannot call `{name}`", node.span)
        if isinstance(callee, ast.FieldExpr):
            return self._emit_method_call(callee, node.args, ctx, w, expect, node.span)
        code, tag = self._emit_expr(callee, ctx, w)
        if isinstance(tag, tuple) and tag[0] == "fn":
            return self._call_closure(code, tag, node.args, ctx, w, node.span)
        raise unsupported("native backend does not support this call form", node.span)

    def _bind_generics(self, texpr, tag, generics: set, out: dict) -> None:
        """Unify a parameter's type expression with an argument's type tag."""
        if isinstance(texpr, ast.RefType):
            self._bind_generics(texpr.inner, tag, generics, out)
        elif isinstance(texpr, ast.TypeName):
            if texpr.name in generics and not texpr.args:
                out.setdefault(texpr.name, tag)
            elif isinstance(tag, tuple):
                kinds = {"Vec": "vec", "Option": "opt", "Result": "res", "Map": "map"}
                if kinds.get(texpr.name) == tag[0]:
                    targs = texpr.args[1:] if tag[0] == "map" else texpr.args
                    for sub, t in zip(targs, tag[1:]):
                        self._bind_generics(sub, t, generics, out)
        elif isinstance(texpr, ast.OptionalType) and isinstance(tag, tuple) and tag[0] == "opt":
            self._bind_generics(texpr.inner, tag[1], generics, out)
        elif isinstance(texpr, ast.ResultType) and isinstance(tag, tuple) and tag[0] == "res":
            self._bind_generics(texpr.ok, tag[1], generics, out)
            if texpr.err is not None:
                self._bind_generics(texpr.err, tag[2], generics, out)
        elif isinstance(texpr, (ast.SliceType, ast.ArrayType)) and isinstance(tag, tuple) \
                and tag[0] == "vec":
            self._bind_generics(texpr.elem, tag[1], generics, out)
        elif isinstance(texpr, ast.TupleType) and isinstance(tag, tuple) and tag[0] == "tup":
            for sub, t in zip(texpr.elems, tag[1:]):
                self._bind_generics(sub, t, generics, out)
        elif isinstance(texpr, ast.FnType) and isinstance(tag, tuple) and tag[0] == "fn":
            if texpr.ret is not None:
                self._bind_generics(texpr.ret, tag[1], generics, out)
            for sub, t in zip(texpr.params, tag[2:]):
                self._bind_generics(sub, t, generics, out)

    def _mentions(self, texpr, names: set) -> bool:
        found: set = set()
        self._free_names_type(texpr, found)
        return bool(found & names)

    def _free_names_type(self, texpr, out: set) -> None:
        if isinstance(texpr, ast.TypeName):
            out.add(texpr.name)
        if isinstance(texpr, ast.Node):
            for f in texpr.__dataclass_fields__:
                if f != "span":
                    v = getattr(texpr, f)
                    for x in (v if isinstance(v, list) else [v]):
                        if isinstance(x, ast.Node):
                            self._free_names_type(x, out)

    def _partial_tag(self, texpr):
        """Like `type_tag`, but unresolved parts of a function type become None
        (a closure argument then infers them, binding type parameters)."""
        try:
            return self.type_tag(texpr)
        except Diagnostic:
            if not isinstance(texpr, ast.FnType):
                return None
        parts = []
        for t in [texpr.ret, *texpr.params]:
            try:
                parts.append(self.type_tag(t) if t is not None else "void")
            except Diagnostic:
                parts.append(None)
        return ("fn", *parts)

    def _emit_generic_call(self, fn: ast.FnDef, args, ctx: FnContext, w: Writer, span):
        """Monomorphize a generic function for the argument types at this call."""
        generics = set(fn.generics)
        params = [p for p in fn.params if not p.is_self]
        positional = [a for a in args if a.name is None]
        named = {a.name: a for a in args if a.name is not None}
        codes: list = []
        bindings: dict = {}
        deferred = []
        for i, p in enumerate(params):
            arg = positional[i] if i < len(positional) else named.get(p.name)
            if arg is None:
                raise unsupported(f"missing argument `{p.name}` in call to `{fn.name}`", span)
            if p.type is None:
                raise unsupported(f"parameter `{p.name}` needs a type annotation", p.span)
            if self._mentions(p.type, generics):
                if isinstance(arg.value, ast.LambdaExpr):
                    deferred.append(i)  # closures need the bindings first
                    codes.append(None)
                    continue
                code, tag = self._emit_expr(arg.value, ctx, w)
                self._bind_generics(p.type, tag, generics, bindings)
            else:
                code, tag = self._emit_expr(arg.value, ctx, w, expect=self.type_tag(p.type))
            codes.append(code)
        saved = self._tenv
        for i in deferred:
            self._tenv = {**saved, **bindings}
            try:
                expect = self._partial_tag(params[i].type)
            finally:
                self._tenv = saved
            arg = positional[i] if i < len(positional) else named[params[i].name]
            code, tag = self._emit_expr(arg.value, ctx, w, expect=expect)
            self._bind_generics(params[i].type, tag, generics, bindings)
            codes[i] = code
        missing = generics - set(bindings)
        if missing:
            raise unsupported(
                f"cannot infer type parameter `{sorted(missing)[0]}` of `{fn.name}` natively", span)
        key = (fn.name, tuple(sorted(bindings.items(), key=lambda kv: kv[0])))
        self._tenv = dict(bindings)
        try:
            if key not in self._specs:
                cname = f"{fn.name}__g{len(self._specs)}"
                self._specs[key] = cname
                self._pending_specs.append((fn, bindings, cname))
                self.spec_protos.append(self._prototype(fn) + ";")
            ret = self.type_tag(fn.return_type)
            self.ensure(ret)
            cname = self._specs[key]
        finally:
            self._tenv = saved
        return f"{cname}({', '.join(codes)})", ret

    def _emit_variant_ctor(self, owner: str, variant: str, args, ctx, w, span):
        edef = self.enums[owner]
        vdef = next(v for v in edef.variants if v.name == variant)
        tag = ("enum", owner)
        self.ensure(tag)
        if not vdef.payload:
            if self._enum_has_payload(edef):
                return f"{owner}_mk_{variant}()", tag
            return f"{owner}__{variant}", tag
        if len(args) != len(vdef.payload):
            raise unsupported(
                f"variant `{variant}` expects {len(vdef.payload)} arguments", span)
        items = []
        for a, (_, ftype) in zip(args, vdef.payload):
            ftag = self.type_tag(ftype)
            code, _ = self._emit_expr(a.value, ctx, w, expect=ftag)
            items.append((code, ftag))
        codes = self._sequence(items, ctx, w)
        return f"{owner}_mk_{variant}({', '.join(codes)})", tag

    def _emit_method_call(self, field: ast.FieldExpr, args: list[ast.Arg],
                          ctx: FnContext, w: Writer, expect=None, span=None):
        recv = field.recv
        mname = field.name
        # static namespaces
        if isinstance(recv, ast.NameExpr) and recv.name not in ctx.var_types \
                and recv.name not in self.const_tags:
            rname = recv.name
            if rname == "Math":
                return self._emit_math(mname, args, ctx, w)
            if rname in self.enums and any(v.name == mname for v in self.enums[rname].variants):
                return self._emit_variant_ctor(rname, mname, args, ctx, w, span)
            if rname in self.methods:
                table = self.methods[rname]
                if mname in table and not (table[mname].params and table[mname].params[0].is_self):
                    m = table[mname]
                    arglist = self._order_args(m, args, ctx, w, span)
                    return (f"{rname}__{mname}({', '.join(arglist)})",
                            self.type_tag(m.return_type))
            if rname == "File":
                return self._emit_file_static(mname, args, ctx, w, span)
            if rname == "Process":
                return self._emit_process(mname, args, ctx, w, span)
            if rname == "Vec" and mname == "new":
                if isinstance(expect, tuple) and expect[0] == "vec":
                    self.ensure(expect)
                    return f"ax_vec_new_{mangle(expect[1])}()", expect
                raise unsupported(
                    "`Vec.new()` needs a type annotation in native code", span)
            if rname == "Map" and mname == "new":
                if isinstance(expect, tuple) and expect[0] == "map":
                    self.ensure(expect)
                    return f"ax_map_new_{mangle(expect[1])}()", expect
                raise unsupported(
                    "`Map.new()` needs a type annotation in native code", span)
        rcode, rtag = self._emit_expr(recv, ctx, w)
        if rtag == "file":
            return self._emit_file_method(rcode, mname, args, ctx, w, span)
        if rtag == "str":
            return self._emit_str_method(rcode, mname, args, ctx, w, span)
        if rtag == "int":
            return self._emit_int_method(rcode, mname, args, ctx, w, span)
        if rtag == "float":
            return self._emit_float_method(rcode, mname, args, ctx, w, span)
        if rtag == "bool":
            if mname == "to_string":
                return f"ax_fmt_bool({rcode})", "str"
        if isinstance(rtag, tuple) and rtag[0] == "vec":
            return self._emit_vec_method(rcode, rtag, mname, args, ctx, w, span)
        if isinstance(rtag, tuple) and rtag[0] == "map":
            return self._emit_map_method(rcode, rtag, mname, args, ctx, w, span)
        if isinstance(rtag, tuple) and rtag[0] == "opt":
            return self._emit_opt_method(rcode, rtag, mname, args, ctx, w, span)
        if isinstance(rtag, tuple) and rtag[0] == "res":
            return self._emit_res_method(rcode, rtag, mname, args, ctx, w, span)
        # user struct / enum methods
        type_name = rtag if isinstance(rtag, str) else (
            rtag[1] if rtag[0] == "enum" else None)
        table = self.methods.get(type_name) if type_name else None
        if table and mname in table:
            m = table[mname]
            is_mut = bool(m.params and m.params[0].is_self and m.params[0].is_mut)
            if not is_mut and args and "(" in rcode:
                tmp = ctx.tmp()
                w.line(f"{self.ctype(rtag)} {tmp} = {rcode};")
                rcode = tmp
            arglist = self._order_args(m, args, ctx, w, span)
            if is_mut:
                recv_arg = f"&({rcode})"
            else:
                recv_arg = rcode
            joined = ", ".join([recv_arg, *arglist])
            return f"{type_name}__{mname}({joined})", self.type_tag(m.return_type)
        if isinstance(rtag, str) and rtag in self.structs:
            self.ensure(rtag)
            ftag = self.struct_fields[rtag].get(mname)
            if isinstance(ftag, tuple) and ftag[0] == "fn":
                return self._call_closure(f"({rcode}).{cfield(mname)}", ftag, args, ctx, w, span)
        if isinstance(rtag, tuple) and rtag[0] == "tup" and mname == "len":
            return str(len(rtag) - 1), "int"
        raise unsupported(
            f"native backend cannot resolve method `{mname}` on `{rtag}`",
            field.span,
        )

    def _one_arg(self, args, ctx, w, expect=None):
        return self._emit_expr(args[0].value, ctx, w, expect=expect)

    def _emit_str_method(self, rcode, mname, args, ctx, w, span):
        simple = {
            "len": ("ax_str_len", "int"),
            "is_empty": ("ax_str_is_empty", "bool"),
            "trim": ("ax_str_trim", "str"),
            "trim_start": ("ax_str_trim_start", "str"),
            "trim_end": ("ax_str_trim_end", "str"),
            "to_upper": ("ax_str_to_upper", "str"),
            "to_uppercase": ("ax_str_to_upper", "str"),
            "to_lower": ("ax_str_to_lower", "str"),
            "to_lowercase": ("ax_str_to_lower", "str"),
        }
        if mname in simple:
            fn, tag = simple[mname]
            return f"{fn}({rcode})", tag
        if mname == "to_string":
            return rcode, "str"
        if mname in {"contains", "starts_with", "ends_with"} and args:
            other, _ = self._one_arg(args, ctx, w, "str")
            return f"ax_str_{mname}({rcode}, {other})", "bool"
        if mname == "compare" and args:
            other, _ = self._one_arg(args, ctx, w, "str")
            return f"(int64_t)ax_str_cmp({rcode}, {other})", "int"
        if mname == "replace" and len(args) == 2:
            a, _ = self._emit_expr(args[0].value, ctx, w, expect="str")
            b, _ = self._emit_expr(args[1].value, ctx, w, expect="str")
            return f"ax_str_replace({rcode}, {a}, {b})", "str"
        if mname == "repeat" and args:
            n, _ = self._one_arg(args, ctx, w, "int")
            return f"ax_str_repeat({rcode}, {n})", "str"
        if mname == "split":
            self.ensure(("vec", "str"))
            if args:
                sep, _ = self._one_arg(args, ctx, w, "str")
                return f"ax_str_split({rcode}, {sep})", ("vec", "str")
            return f"ax_str_split_ws({rcode})", ("vec", "str")
        if mname == "lines":
            self.ensure(("vec", "str"))
            return f"ax_str_lines({rcode})", ("vec", "str")
        if mname == "chars":
            self.ensure(("vec", "str"))
            return f"ax_str_chars({rcode})", ("vec", "str")
        if mname == "slice" and args:
            idx = args[0].value
            if isinstance(idx, ast.RangeExpr):
                return self._emit_str_slice(rcode, idx, ctx, w)
            i, _ = self._one_arg(args, ctx, w, "int")
            return f"ax_str_index({rcode}, {i})", "str"
        if mname == "find" and args:
            other, _ = self._one_arg(args, ctx, w, "str")
            tag = ("opt", "int")
            self.ensure(tag)
            pos, out = ctx.tmp(), ctx.tmp()
            w.line(f"int64_t {pos} = ax_str_find({rcode}, {other});")
            w.line(f"{self.ctype(tag)} {out} = {pos} >= 0 ? ax_opt_some_int({pos}) : ax_opt_none_int();")
            return out, tag
        if mname in {"strip_prefix", "strip_suffix"} and args:
            src, other = ctx.tmp(), ctx.tmp()
            ocode, _ = self._one_arg(args, ctx, w, "str")
            w.line(f"AxStr {src} = {rcode};")
            w.line(f"AxStr {other} = {ocode};")
            tag = ("opt", "str")
            self.ensure(tag)
            if mname == "strip_prefix":
                test = f"ax_str_starts_with({src}, {other})"
                rest = f"ax_str_slice({src}, {other}.len, {src}.len)"
            else:
                test = f"ax_str_ends_with({src}, {other})"
                rest = f"ax_str_slice({src}, 0, {src}.len - {other}.len)"
            return f"({test} ? ax_opt_some_str({rest}) : ax_opt_none_str())", tag
        if mname == "split_once" and args:
            src, sep, pos = ctx.tmp(), ctx.tmp(), ctx.tmp()
            ocode, _ = self._one_arg(args, ctx, w, "str")
            pair = ("tup", "str", "str")
            tag = ("opt", pair)
            self.ensure(tag)
            w.line(f"AxStr {src} = {rcode};")
            w.line(f"AxStr {sep} = {ocode};")
            w.line(f"int64_t {pos} = {sep}.len ? ax_str_find({src}, {sep}) : -1;")
            out = ctx.tmp()
            w.line(f"{self.ctype(tag)} {out} = ax_opt_none_{mangle(pair)}();")
            w.line(f"if ({pos} >= 0) {out} = ax_opt_some_{mangle(pair)}(({self.ctype(pair)}){{ "
                   f"ax_str_slice({src}, 0, {pos}), ax_str_slice({src}, {pos} + {sep}.len, {src}.len) }});")
            return out, tag
        if mname == "bytes":
            tag = ("vec", "int")
            self.ensure(tag)
            src, out, i = ctx.tmp(), ctx.tmp(), ctx.tmp()
            w.line(f"AxStr {src} = {rcode};")
            w.line(f"{self.ctype(tag)} {out} = ax_vec_new_int();")
            w.line(f"for (int64_t {i} = 0; {i} < {src}.len; {i}++) "
                   f"ax_vec_push_int(&{out}, (int64_t)(unsigned char){src}.data[{i}]);")
            return out, tag
        if mname == "parse" and args and isinstance(args[0].value, ast.NameExpr):
            ty = args[0].value.name
            if ty in _INT_TYPES:
                return self._emit_parse(rcode, "int", ctx, w)
            if ty in _FLOAT_TYPES:
                return self._emit_parse(rcode, "float", ctx, w)
            if ty == "bool":
                rtag = ("res", "bool", "str")
                self.ensure(rtag)
                return (f"ax_res_ok_{mangle(rtag)}(ax_str_eq(ax_str_trim({rcode}), "
                        f'ax_str_lit("true")))', rtag)
        raise unsupported(f"native backend does not support str.{mname} yet", span)

    def _emit_parse(self, rcode, target, ctx, w):
        rtag = ("res", target, "str")
        self.ensure(rtag)
        tmp = ctx.tmp()
        out = ctx.tmp()
        ct = self.ctype(target)
        fn = "ax_str_parse_int" if target == "int" else "ax_str_parse_float"
        w.line(f"{ct} {tmp} = 0;")
        w.line(f"{self.ctype(rtag)} {out};")
        w.line(f"if ({fn}({rcode}, &{tmp})) {out} = ax_res_ok_{mangle(rtag)}({tmp});")
        w.line(f'else {out} = ax_res_err_{mangle(rtag)}(ax_str_lit("parse error"));')
        return out, rtag

    def _emit_str_slice(self, rcode, rng: ast.RangeExpr, ctx, w):
        start = self._emit_expr(rng.start, ctx, w, expect="int")[0] if rng.start else "0"
        if rng.end is not None:
            end = self._emit_expr(rng.end, ctx, w, expect="int")[0]
            if rng.inclusive:
                end = f"({end}) + 1"
        else:
            end = f"({rcode}).len"
        return f"ax_str_slice({rcode}, {start}, {end})", "str"

    def _emit_int_method(self, rcode, mname, args, ctx, w, span):
        if mname == "to_string":
            return f"ax_fmt_int({rcode})", "str"
        if mname == "to_float":
            return f"((double)({rcode}))", "float"
        if mname == "abs":
            return f"ax_iabs({rcode})", "int"
        if mname in {"max", "min"} and args:
            other, _ = self._one_arg(args, ctx, w, "int")
            return f"ax_i{mname}({rcode}, {other})", "int"
        if mname == "clamp" and len(args) == 2:
            lo, _ = self._emit_expr(args[0].value, ctx, w, expect="int")
            hi, _ = self._emit_expr(args[1].value, ctx, w, expect="int")
            return f"ax_iclamp({rcode}, {lo}, {hi})", "int"
        if mname == "pow" and args:
            e, _ = self._one_arg(args, ctx, w, "int")
            return f"ax_ipow({rcode}, {e})", "int"
        if mname == "signum":
            return f"ax_isignum({rcode})", "int"
        if mname in {"is_even", "is_odd"}:
            return f"((({rcode}) % 2 {'==' if mname == 'is_even' else '!='} 0))", "bool"
        raise unsupported(f"native backend does not support int.{mname} yet", span)

    def _emit_float_method(self, rcode, mname, args, ctx, w, span):
        if mname == "to_string":
            return f"ax_fmt_float({rcode})", "str"
        if mname == "to_int":
            return f"ax_f2i({rcode})", "int"
        if mname in {"floor", "ceil"}:
            return f"ax_f2i({mname}({rcode}))", "int"
        if mname == "round":
            return f"ax_fround({rcode})", "int"
        if mname == "sqrt":
            return f"ax_fsqrt({rcode})", "float"
        if mname == "is_nan":
            return f"isnan({rcode})", "bool"
        if mname == "pow" and args:
            e, _ = self._one_arg(args, ctx, w, "float")
            return f"pow({rcode}, {e})", "float"
        if mname == "abs":
            return f"fabs({rcode})", "float"
        if mname in {"max", "min"} and args:
            other, _ = self._one_arg(args, ctx, w, "float")
            return f"ax_f{mname}_({rcode}, {other})", "float"
        raise unsupported(f"native backend does not support float.{mname} yet", span)

    def _emit_vec_method(self, rcode, rtag, mname, args, ctx, w, span):
        elem = rtag[1]
        s = mangle(elem)
        if mname == "len":
            return f"({rcode}).len", "int"
        if mname == "is_empty":
            return f"(({rcode}).len == 0)", "bool"
        if mname == "push" and args:
            v, _ = self._one_arg(args, ctx, w, elem)
            w.line(f"ax_vec_push_{s}(&({rcode}), {v});")
            return "((void)0)", "void"
        if mname == "pop":
            return f"ax_vec_pop_{s}(&({rcode}))", ("opt", elem)
        if mname == "first":
            return f"ax_vec_first_{s}({rcode})", ("opt", elem)
        if mname == "last":
            return f"ax_vec_last_{s}({rcode})", ("opt", elem)
        if mname == "get" and args:
            i, _ = self._one_arg(args, ctx, w, "int")
            return f"ax_vec_get_opt_{s}({rcode}, {i})", ("opt", elem)
        if mname == "contains" and args:
            if self._eq_fn(elem) is None:
                raise unsupported(
                    f"Vec.contains is not supported for `{elem}` natively", span)
            v, _ = self._one_arg(args, ctx, w, elem)
            return f"ax_vec_contains_{s}({rcode}, {v})", "bool"
        if mname == "reverse":
            return f"ax_vec_reverse_{s}({rcode})", rtag
        if mname in {"to_vec", "collect"}:
            return rcode, rtag
        if mname == "sort":
            if elem not in {"int", "float", "str"}:
                raise unsupported(
                    f"Vec.sort is not supported for `{elem}` natively", span)
            return f"ax_vec_sort_{s}({rcode})", rtag
        if mname == "sum":
            if elem not in {"int", "float"}:
                raise unsupported("Vec.sum requires numeric elements", span)
            return f"ax_vec_sum_{s}({rcode})", elem
        if mname == "join":
            sep = self._one_arg(args, ctx, w, "str")[0] if args else 'ax_str_lit("")'
            return self._emit_vec_join(rcode, elem, sep, ctx, w)
        if mname == "slice" and args and isinstance(args[0].value, ast.RangeExpr):
            rng = args[0].value
            start = self._emit_expr(rng.start, ctx, w, expect="int")[0] if rng.start else "0"
            if rng.end is not None:
                end = self._emit_expr(rng.end, ctx, w, expect="int")[0]
                if rng.inclusive:
                    end = f"({end}) + 1"
            else:
                end = f"({rcode}).len"
            return f"ax_vec_slice_{s}({rcode}, {start}, {end})", rtag
        return self._emit_vec_method_ext(rcode, rtag, mname, args, ctx, w, span)

    def _emit_vec_method_ext(self, rcode, rtag, mname, args, ctx, w, span):
        """Vec methods beyond the core set, including the higher-order ones
        that take closures. Each lowers to an inline loop."""
        elem = rtag[1]
        s = mangle(elem)
        ct, cv = self.ctype(elem), self.ctype(rtag)
        if mname == "insert" and len(args) == 2:
            i, _ = self._emit_expr(args[0].value, ctx, w, expect="int")
            v, _ = self._emit_expr(args[1].value, ctx, w, expect=elem)
            w.line(f"ax_vec_insert_{s}(&({rcode}), {i}, {v});")
            return "((void)0)", "void"
        if mname == "remove" and args:
            i, _ = self._one_arg(args, ctx, w, "int")
            out = ctx.tmp()
            w.line(f"{ct} {out} = ax_vec_remove_{s}(&({rcode}), {i});")
            return out, elem
        if mname == "clear":
            w.line(f"({rcode}).len = 0;")
            return "((void)0)", "void"
        if mname == "extend" and args:
            other, _ = self._one_arg(args, ctx, w, rtag)
            o, i = ctx.tmp(), ctx.tmp()
            w.line(f"{cv} {o} = {other};")
            w.line(f"for (int64_t {i} = 0; {i} < {o}.len; {i}++) ax_vec_push_{s}(&({rcode}), {o}.data[{i}]);")
            return "((void)0)", "void"
        if mname in {"iter", "clone"}:
            return f"ax_vec_slice_{s}({rcode}, 0, ({rcode}).len)", rtag
        if mname == "get_mut" and args:
            i, _ = self._one_arg(args, ctx, w, "int")
            return f"ax_vec_get_opt_{s}({rcode}, {i})", ("opt", elem)
        src = ctx.tmp()
        self.ensure(rtag)
        w.line(f"{cv} {src} = {rcode};")
        i = ctx.tmp()
        loop = f"for (int64_t {i} = 0; {i} < {src}.len; {i}++)"
        x = f"{src}.data[{i}]"
        if mname in {"take", "skip"} and args:
            n, _ = self._one_arg(args, ctx, w, "int")
            if mname == "take":
                return f"ax_vec_slice_{s}({src}, 0, ax_imax({n}, 0))", rtag
            return f"ax_vec_slice_{s}({src}, ax_imax({n}, 0), {src}.len)", rtag
        if mname in {"index_of", "dedup"}:
            eq = self._eq_fn(elem)
            if eq is None:
                raise unsupported(f"Vec.{mname} needs comparable elements natively", span)
            if mname == "index_of":
                needle, _ = self._one_arg(args, ctx, w, elem)
                nt, out = ctx.tmp(), ctx.tmp()
                tag = ("opt", "int")
                self.ensure(tag)
                w.line(f"{ct} {nt} = {needle};")
                w.line(f"{self.ctype(tag)} {out} = ax_opt_none_int();")
                w.line(f"{loop} if ({eq(x, nt)}) {{ {out} = ax_opt_some_int({i}); break; }}")
                return out, tag
            out = ctx.tmp()
            w.line(f"{cv} {out} = ax_vec_new_{s}();")
            w.line(f"{loop} if ({out}.len == 0 || !({eq(f'{out}.data[{out}.len - 1]', x)})) "
                   f"ax_vec_push_{s}(&{out}, {x});")
            return out, rtag
        if mname in {"min", "max"}:
            if elem not in {"int", "float", "str"}:
                raise unsupported(f"Vec.{mname} needs int, float, or str elements natively", span)
            tag = ("opt", elem)
            out = ctx.tmp()
            better = (f"ax_str_cmp({x}, {out}.val) {'<' if mname == 'min' else '>'} 0"
                      if elem == "str" else f"{x} {'<' if mname == 'min' else '>'} {out}.val")
            w.line(f"{self.ctype(tag)} {out} = ax_opt_none_{s}();")
            w.line(f"{loop} if (!{out}.has || {better}) {out} = ax_opt_some_{s}({x});")
            return out, tag
        if mname == "enumerate":
            pair = ("tup", "int", elem)
            tag = ("vec", pair)
            self.ensure(tag)
            out = ctx.tmp()
            w.line(f"{self.ctype(tag)} {out} = ax_vec_new_{mangle(pair)}();")
            w.line(f"{loop} ax_vec_push_{mangle(pair)}(&{out}, (({self.ctype(pair)}){{ {i}, {x} }}));")
            return out, tag
        if mname == "zip" and args:
            other, otag = self._one_arg(args, ctx, w)
            if not (isinstance(otag, tuple) and otag[0] == "vec"):
                raise unsupported("Vec.zip expects a Vec", span)
            pair = ("tup", elem, otag[1])
            tag = ("vec", pair)
            self.ensure(tag)
            o, out = ctx.tmp(), ctx.tmp()
            w.line(f"{self.ctype(otag)} {o} = {other};")
            w.line(f"{self.ctype(tag)} {out} = ax_vec_new_{mangle(pair)}();")
            w.line(f"for (int64_t {i} = 0; {i} < {src}.len && {i} < {o}.len; {i}++) "
                   f"ax_vec_push_{mangle(pair)}(&{out}, (({self.ctype(pair)}){{ {x}, {o}.data[{i}] }}));")
            return out, tag
        if mname in {"windows", "chunks"} and args:
            n, _ = self._one_arg(args, ctx, w, "int")
            nt, out = ctx.tmp(), ctx.tmp()
            tag = ("vec", rtag)
            self.ensure(tag)
            what = "window size" if mname == "windows" else "chunk size"
            w.line(f"int64_t {nt} = {n};")
            w.line(f'if ({nt} <= 0) ax_panic(ax_str_lit("{what} must be positive"));')
            sv = mangle(rtag)
            w.line(f"{self.ctype(tag)} {out} = ax_vec_new_{sv}();")
            if mname == "windows":
                w.line(f"for (int64_t {i} = 0; {i} + {nt} <= {src}.len; {i}++) "
                       f"ax_vec_push_{sv}(&{out}, ax_vec_slice_{s}({src}, {i}, {i} + {nt}));")
            else:
                w.line(f"for (int64_t {i} = 0; {i} < {src}.len; {i} += {nt}) "
                       f"ax_vec_push_{sv}(&{out}, ax_vec_slice_{s}({src}, {i}, {i} + {nt}));")
            return out, tag
        # higher-order methods
        if mname == "map" and args:
            fn, ftag = self._closure_arg(args[0], None, [elem], ctx, w, span, "Vec.map")
            rt = ftag[1]
            if rt == "void":
                raise unsupported("Vec.map needs a closure that returns a value", span)
            tag = ("vec", rt)
            self.ensure(tag)
            out = ctx.tmp()
            w.line(f"{self.ctype(tag)} {out} = ax_vec_new_{mangle(rt)}();")
            w.line(f"{loop} ax_vec_push_{mangle(rt)}(&{out}, {self._invoke(fn, x)});")
            return out, tag
        if mname == "filter" and args:
            fn, ftag = self._closure_arg(args[0], "bool", [elem], ctx, w, span, "Vec.filter")
            out = ctx.tmp()
            w.line(f"{cv} {out} = ax_vec_new_{s}();")
            w.line(f"{loop} if ({self._truthy(self._invoke(fn, x), ftag[1])}) ax_vec_push_{s}(&{out}, {x});")
            return out, rtag
        if mname == "fold" and len(args) == 2:
            init, itag = self._emit_expr(args[0].value, ctx, w)
            self.ensure(itag)
            fn, ftag = self._closure_arg(args[1], itag, [itag, elem], ctx, w, span, "Vec.fold")
            acc = ctx.tmp()
            w.line(f"{self.ctype(itag)} {acc} = {init};")
            w.line(f"{loop} {acc} = {self._invoke(fn, acc, x)};")
            return acc, itag
        if mname in {"any", "all", "count", "find", "position", "for_each"} and args:
            ret = None if mname == "for_each" else "bool"
            fn, ftag = self._closure_arg(args[0], ret, [elem], ctx, w, span, f"Vec.{mname}")
            hit = self._truthy(self._invoke(fn, x), ftag[1]) if mname != "for_each" else None
            out = ctx.tmp()
            if mname == "for_each":
                w.line(f"{loop} {self._invoke(fn, x)};")
                return "((void)0)", "void"
            if mname == "any":
                w.line(f"bool {out} = false;")
                w.line(f"{loop} if ({hit}) {{ {out} = true; break; }}")
                return out, "bool"
            if mname == "all":
                w.line(f"bool {out} = true;")
                w.line(f"{loop} if (!({hit})) {{ {out} = false; break; }}")
                return out, "bool"
            if mname == "count":
                w.line(f"int64_t {out} = 0;")
                w.line(f"{loop} if ({hit}) {out}++;")
                return out, "int"
            if mname == "find":
                tag = ("opt", elem)
                w.line(f"{self.ctype(tag)} {out} = ax_opt_none_{s}();")
                w.line(f"{loop} if ({hit}) {{ {out} = ax_opt_some_{s}({x}); break; }}")
                return out, tag
            tag = ("opt", "int")
            self.ensure(tag)
            w.line(f"{self.ctype(tag)} {out} = ax_opt_none_int();")
            w.line(f"{loop} if ({hit}) {{ {out} = ax_opt_some_int({i}); break; }}")
            return out, tag
        if mname == "sort_by" and args:
            fn, ftag = self._closure_arg(args[0], "int", [elem, elem], ctx, w, span, "Vec.sort_by")
            helper = self._ensure_sort_by(rtag, ftag)
            return f"{helper}({src}, {fn})", rtag
        raise unsupported(f"native backend does not support Vec.{mname} yet", span)

    def _ensure_sort_by(self, rtag, ftag) -> str:
        """A stable bottom-up merge sort driven by a comparator closure (the
        interpreter's `sorted` is stable too, so equal keys keep their order)."""
        name = f"ax_vec_sort_by_{mangle(rtag)}_{mangle(ftag)}"
        if name in self._helpers:
            return name
        self._helpers.add(name)
        cv, ct, cf = self.ctype(rtag), self.ctype(rtag[1]), self.ctype(ftag)
        s = mangle(rtag[1])
        self.lambda_defs.append(
            f"static {cv} {name}({cv} v, {cf} cmp) {{\n"
            f"    {cv} out = ax_vec_slice_{s}(v, 0, v.len);\n"
            f"    int64_t n = out.len;\n"
            f"    if (n < 2) return out;\n"
            f"    {ct} *buf = ({ct} *)ax_alloc((size_t)n * sizeof({ct}));\n"
            f"    for (int64_t width = 1; width < n; width *= 2) {{\n"
            f"        for (int64_t lo = 0; lo < n; lo += 2 * width) {{\n"
            f"            int64_t mid = lo + width < n ? lo + width : n;\n"
            f"            int64_t hi = lo + 2 * width < n ? lo + 2 * width : n;\n"
            f"            int64_t a = lo, b = mid, k = lo;\n"
            f"            while (a < mid && b < hi)\n"
            f"                buf[k++] = cmp.call(cmp.env, out.data[b], out.data[a]) < 0\n"
            f"                    ? out.data[b++] : out.data[a++];\n"
            f"            while (a < mid) buf[k++] = out.data[a++];\n"
            f"            while (b < hi) buf[k++] = out.data[b++];\n"
            f"        }}\n"
            f"        memcpy(out.data, buf, (size_t)n * sizeof({ct}));\n"
            f"    }}\n"
            f"    free(buf);\n"
            f"    return out;\n}}")
        return name

    def _emit_vec_join(self, rcode, elem, sep, ctx, w):
        show = self.ensure_show(elem) if elem != "str" else None
        vec_tmp, out, idx = ctx.tmp(), ctx.tmp(), ctx.tmp()
        self.ensure(("vec", elem))
        w.line(f"{self.ctype(('vec', elem))} {vec_tmp} = {rcode};")
        w.line(f'AxStr {out} = ax_str_lit("");')
        w.line(f"for (int64_t {idx} = 0; {idx} < {vec_tmp}.len; {idx}++) {{")
        w.indent()
        w.line(f"if ({idx}) {out} = ax_str_concat({out}, {sep});")
        piece = f"{vec_tmp}.data[{idx}]"
        if show:
            piece = f"{show}({piece})"
        w.line(f"{out} = ax_str_concat({out}, {piece});")
        w.dedent()
        w.line("}")
        return out, "str"

    def _emit_map_method(self, rcode, rtag, mname, args, ctx, w, span):
        s = mangle(rtag[1])
        if mname == "len":
            return f"({rcode}).keys.len", "int"
        if mname == "is_empty":
            return f"(({rcode}).keys.len == 0)", "bool"
        if mname in {"get", "get_mut"} and args:
            k, _ = self._one_arg(args, ctx, w, "str")
            return f"ax_map_get_{s}({rcode}, {k})", ("opt", rtag[1])
        if mname == "insert" and len(args) == 2:
            k, _ = self._emit_expr(args[0].value, ctx, w, expect="str")
            v, _ = self._emit_expr(args[1].value, ctx, w, expect=rtag[1])
            w.line(f"ax_map_insert_{s}(&({rcode}), {k}, {v});")
            return "((void)0)", "void"
        if mname == "remove" and args:
            k, _ = self._one_arg(args, ctx, w, "str")
            return f"ax_map_remove_{s}(&({rcode}), {k})", ("opt", rtag[1])
        if mname == "contains_key" and args:
            k, _ = self._one_arg(args, ctx, w, "str")
            return f"ax_map_contains_{s}({rcode}, {k})", "bool"
        if mname == "keys":
            return f"ax_vec_slice_str(({rcode}).keys, 0, ({rcode}).keys.len)", ("vec", "str")
        if mname == "values":
            return f"ax_vec_slice_{s}(({rcode}).vals, 0, ({rcode}).vals.len)", ("vec", rtag[1])
        if mname == "clear":
            w.line(f"({rcode}).keys.len = 0; ({rcode}).vals.len = 0;")
            return "((void)0)", "void"
        if mname in {"entries", "iter"}:
            pair = ("tup", "str", rtag[1])
            tag = ("vec", pair)
            self.ensure(tag)
            m, out, i = ctx.tmp(), ctx.tmp(), ctx.tmp()
            w.line(f"{self.ctype(rtag)} {m} = {rcode};")
            w.line(f"{self.ctype(tag)} {out} = ax_vec_new_{mangle(pair)}();")
            w.line(f"for (int64_t {i} = 0; {i} < {m}.keys.len; {i}++) ax_vec_push_{mangle(pair)}"
                   f"(&{out}, (({self.ctype(pair)}){{ {m}.keys.data[{i}], {m}.vals.data[{i}] }}));")
            return out, tag
        raise unsupported(f"native backend does not support Map.{mname} yet", span)

    def _emit_opt_method(self, rcode, rtag, mname, args, ctx, w, span):
        s = mangle(rtag[1])
        if mname == "is_some":
            return f"({rcode}).has", "bool"
        if mname == "is_none":
            return f"(!({rcode}).has)", "bool"
        if mname == "unwrap":
            return f"ax_opt_unwrap_{s}({rcode})", rtag[1]
        if mname == "unwrap_or" and args:
            fallback, _ = self._one_arg(args, ctx, w, rtag[1])
            tmp = ctx.tmp()
            w.line(f"{self.ctype(rtag)} {tmp} = {rcode};")
            return f"({tmp}.has ? {tmp}.val : {fallback})", rtag[1]
        if mname == "expect" and args:
            msg = self._to_str(w, *self._emit_expr(args[0].value, ctx, w))
            return f"ax_opt_expect_{s}({rcode}, {msg})", rtag[1]
        t = rtag[1]
        src = ctx.tmp()
        w.line(f"{self.ctype(rtag)} {src} = {rcode};")
        if mname == "or" and args:
            other, _ = self._one_arg(args, ctx, w, rtag)
            out = ctx.tmp()
            w.line(f"{self.ctype(rtag)} {out} = {src};")
            w.line(f"if (!{out}.has) {out} = {other};")
            return out, rtag
        if mname == "ok_or" and args:
            e, etag = self._one_arg(args, ctx, w)
            tag = ("res", t, etag)
            self.ensure(tag)
            out = ctx.tmp()
            w.line(f"{self.ctype(tag)} {out};")
            w.line(f"if ({src}.has) {out} = ax_res_ok_{mangle(tag)}({src}.val); "
                   f"else {out} = ax_res_err_{mangle(tag)}({e});")
            return out, tag
        if mname == "unwrap_or_else" and args:
            fn, ftag = self._closure_arg(args[0], t, [], ctx, w, span, "Option.unwrap_or_else")
            out = ctx.tmp()
            w.line(f"{self.ctype(t)} {out} = {src}.has ? {src}.val : {self._invoke(fn)};")
            return out, t
        if mname in {"map", "and_then", "filter"} and args:
            want = {"map": None, "and_then": None, "filter": "bool"}[mname]
            fn, ftag = self._closure_arg(args[0], want, [t], ctx, w, span, f"Option.{mname}")
            out = ctx.tmp()
            if mname == "filter":
                w.line(f"{self.ctype(rtag)} {out} = ax_opt_none_{s}();")
                w.line(f"if ({src}.has && {self._truthy(self._invoke(fn, f'{src}.val'), ftag[1])}) "
                       f"{out} = {src};")
                return out, rtag
            if mname == "map":
                tag = ("opt", ftag[1])
                self.ensure(tag)
                w.line(f"{self.ctype(tag)} {out} = ax_opt_none_{mangle(ftag[1])}();")
                w.line(f"if ({src}.has) {out} = ax_opt_some_{mangle(ftag[1])}"
                       f"({self._invoke(fn, f'{src}.val')});")
                return out, tag
            tag = ftag[1]
            if not (isinstance(tag, tuple) and tag[0] == "opt"):
                raise unsupported("Option.and_then needs a closure returning an Option", span)
            w.line(f"{self.ctype(tag)} {out} = ax_opt_none_{mangle(tag[1])}();")
            w.line(f"if ({src}.has) {out} = {self._invoke(fn, f'{src}.val')};")
            return out, tag
        raise unsupported(f"native backend does not support Option.{mname} yet", span)

    def _emit_res_method(self, rcode, rtag, mname, args, ctx, w, span):
        s = mangle(rtag)
        if mname == "is_ok":
            return f"({rcode}).ok", "bool"
        if mname == "is_err":
            return f"(!({rcode}).ok)", "bool"
        if mname == "unwrap":
            return f"ax_res_unwrap_{s}({rcode})", rtag[1]
        if mname == "unwrap_err":
            return f"ax_res_unwrap_err_{s}({rcode})", rtag[2]
        if mname == "unwrap_or" and args:
            fallback, _ = self._one_arg(args, ctx, w, rtag[1])
            tmp = ctx.tmp()
            w.line(f"{self.ctype(rtag)} {tmp} = {rcode};")
            return f"({tmp}.ok ? {tmp}.val : {fallback})", rtag[1]
        if mname == "ok":
            otag = ("opt", rtag[1])
            self.ensure(otag)
            tmp = ctx.tmp()
            w.line(f"{self.ctype(rtag)} {tmp} = {rcode};")
            return (f"({tmp}.ok ? ax_opt_some_{mangle(rtag[1])}({tmp}.val) "
                    f": ax_opt_none_{mangle(rtag[1])}())", otag)
        t, e = rtag[1], rtag[2]
        src = ctx.tmp()
        w.line(f"{self.ctype(rtag)} {src} = {rcode};")
        if mname == "err":
            otag = ("opt", e)
            self.ensure(otag)
            return (f"({src}.ok ? ax_opt_none_{mangle(e)}() : ax_opt_some_{mangle(e)}({src}.err))",
                    otag)
        if mname == "expect" and args:
            msg = self._to_str(w, *self._emit_expr(args[0].value, ctx, w))
            show = self.ensure_show(e)
            w.line(f"if (!{src}.ok) ax_panic(ax_str_concat(ax_str_concat({msg}, "
                   f'ax_str_lit(": ")), {show}({src}.err)));')
            return f"{src}.val", t
        if mname == "unwrap_or_else" and args:
            fn, ftag = self._closure_arg(args[0], t, [e], ctx, w, span, "Result.unwrap_or_else")
            out = ctx.tmp()
            w.line(f"{self.ctype(t)} {out} = {src}.ok ? {src}.val : {self._invoke(fn, f'{src}.err')};")
            return out, t
        if mname in {"map", "map_err", "and_then"} and args:
            arg_t = e if mname == "map_err" else t
            fn, ftag = self._closure_arg(args[0], None, [arg_t], ctx, w, span, f"Result.{mname}")
            out = ctx.tmp()
            if mname == "and_then":
                tag = ftag[1]
                if not (isinstance(tag, tuple) and tag[0] == "res"):
                    raise unsupported("Result.and_then needs a closure returning a Result", span)
                w.line(f"{self.ctype(tag)} {out};")
                w.line(f"if ({src}.ok) {out} = {self._invoke(fn, f'{src}.val')}; "
                       f"else {out} = ax_res_err_{mangle(tag)}({src}.err);")
                return out, tag
            tag = ("res", ftag[1], e) if mname == "map" else ("res", t, ftag[1])
            self.ensure(tag)
            w.line(f"{self.ctype(tag)} {out};")
            if mname == "map":
                w.line(f"if ({src}.ok) {out} = ax_res_ok_{mangle(tag)}({self._invoke(fn, f'{src}.val')}); "
                       f"else {out} = ax_res_err_{mangle(tag)}({src}.err);")
            else:
                w.line(f"if ({src}.ok) {out} = ax_res_ok_{mangle(tag)}({src}.val); "
                       f"else {out} = ax_res_err_{mangle(tag)}({self._invoke(fn, f'{src}.err')});")
            return out, tag
        raise unsupported(f"native backend does not support Result.{mname} yet", span)

    def _emit_process(self, mname, args, ctx, w, span):
        if mname == "args" and not args:
            tag = ("vec", "str")
            self.ensure(tag)
            out, i = ctx.tmp(), ctx.tmp()
            w.line(f"{self.ctype(tag)} {out} = ax_vec_new_str();")
            w.line(f"for (int {i} = 1; {i} < ax_argc; {i}++) "
                   f"ax_vec_push_str(&{out}, ax_str_lit(ax_argv[{i}]));")
            return out, tag
        if mname == "exit":
            code = self._one_arg(args, ctx, w, "int")[0] if args else "0"
            w.line(f"ax_exit({code});")
            return "((void)0)", "void"
        if mname == "env" and args:
            name, _ = self._one_arg(args, ctx, w, "str")
            tag = ("opt", "str")
            self.ensure(tag)
            val, out = ctx.tmp(), ctx.tmp()
            w.line(f"AxStr {val};")
            w.line(f"{self.ctype(tag)} {out} = ax_getenv({name}, &{val}) "
                   f"? ax_opt_some_str({val}) : ax_opt_none_str();")
            return out, tag
        raise unsupported(f"native backend does not support Process.{mname}", span)

    def _emit_file_static(self, mname, args, ctx, w, span):
        if mname == "open" and args:
            p, _ = self._one_arg(args, ctx, w, "str")
            return f"ax_file_open({p})", "file"
        if mname == "exists" and args:
            p, _ = self._one_arg(args, ctx, w, "str")
            return f"ax_file_exists({p})", "bool"
        if mname == "read_to_string" and args:
            p, _ = self._one_arg(args, ctx, w, "str")
            rtag = ("res", "str", "str")
            self.ensure(rtag)
            tmp, out = ctx.tmp(), ctx.tmp()
            w.line(f'AxStr {tmp} = ax_str_lit("");')
            w.line(f"{self.ctype(rtag)} {out};")
            w.line(f"if (ax_read_file({p}, &{tmp})) {out} = ax_res_ok_{mangle(rtag)}({tmp});")
            w.line(f'else {out} = ax_res_err_{mangle(rtag)}(ax_str_lit("cannot read file"));')
            return out, rtag
        if mname == "write" and len(args) == 2:
            p, _ = self._emit_expr(args[0].value, ctx, w, expect="str")
            data, _ = self._emit_expr(args[1].value, ctx, w, expect="str")
            rtag = ("res", "bool", "str")
            self.ensure(rtag)
            out = ctx.tmp()
            w.line(f"{self.ctype(rtag)} {out};")
            w.line(f"if (ax_write_file({p}, {data})) {out} = ax_res_ok_{mangle(rtag)}(true);")
            w.line(f'else {out} = ax_res_err_{mangle(rtag)}(ax_str_lit("cannot write file"));')
            return out, rtag
        raise unsupported(f"native backend does not support File.{mname} yet", span)

    def _emit_file_method(self, rcode, mname, args, ctx, w, span):
        if mname in {"read_all_text", "read_to_string"}:
            return f"ax_file_read_all({rcode})", "str"
        if mname == "read_lines":
            self.ensure(("vec", "str"))
            return f"ax_str_lines(ax_file_read_all({rcode}))", ("vec", "str")
        if mname in {"write_all", "write"} and args:
            data = self._to_str(w, *self._emit_expr(args[0].value, ctx, w))
            w.line(f"if (!ax_write_file({rcode}, {data})) "
                   f'ax_panic(ax_str_concat(ax_str_lit("cannot write file: "), {rcode}));')
            return "((void)0)", "void"
        raise unsupported(f"native backend does not support File.{mname} yet", span)

    def _emit_math(self, mname: str, args, ctx, w):
        cmap = {"sqrt": "ax_fsqrt", "sin": "sin", "cos": "cos", "tan": "tan",
                "log": "ax_flog", "exp": "exp", "pow": "pow"}
        if mname in cmap:
            arglist = ", ".join(
                self._emit_expr(a.value, ctx, w, expect="float")[0] for a in args)
            return f"{cmap[mname]}({arglist})", "float"
        if mname in {"floor", "ceil"}:
            a, _ = self._emit_expr(args[0].value, ctx, w, expect="float")
            return f"ax_f2i({mname}({a}))", "int"
        if mname == "round":
            a, _ = self._emit_expr(args[0].value, ctx, w, expect="float")
            return f"ax_fround({a})", "int"
        if mname == "abs":
            a, atag = self._emit_expr(args[0].value, ctx, w)
            return (f"ax_iabs({a})", "int") if atag == "int" else (f"fabs({a})", "float")
        if mname in {"max", "min"}:
            a, atag = self._emit_expr(args[0].value, ctx, w)
            b, _ = self._emit_expr(args[1].value, ctx, w, expect=atag)
            if atag == "int":
                return f"ax_i{mname}({a}, {b})", "int"
            return f"ax_f{mname}_({a}, {b})", "float"
        raise unsupported(f"native backend does not support Math.{mname}")

    def _emit_field(self, node: ast.FieldExpr, ctx: FnContext, w: Writer):
        if isinstance(node.recv, ast.NameExpr) and node.recv.name not in ctx.var_types:
            rname = node.recv.name
            if rname == "Math":
                consts = {"PI": "3.141592653589793", "E": "2.718281828459045",
                          "TAU": "6.283185307179586"}
                if node.name in consts:
                    return consts[node.name], "float"
            if rname in self.enums:
                edef = self.enums[rname]
                vdef = next((v for v in edef.variants if v.name == node.name), None)
                if vdef is not None:
                    if vdef.payload:
                        raise unsupported(
                            f"variant `{node.name}` needs payload arguments", node.span)
                    tag = ("enum", rname)
                    self.ensure(tag)
                    if self._enum_has_payload(edef):
                        return f"{rname}_mk_{node.name}()", tag
                    return f"{rname}__{node.name}", tag
        code, tag = self._emit_expr(node.recv, ctx, w)
        if isinstance(tag, str) and tag in self.structs:
            self.ensure(tag)
            fields = self.struct_fields.get(tag, {})
            if node.name in fields:
                return f"({code}).{cfield(node.name)}", fields[node.name]
        if isinstance(tag, tuple) and tag[0] == "tup" and node.name.isdigit():
            i = int(node.name)
            if i + 1 < len(tag):
                return f"({code}).f{i}", tag[i + 1]
        raise unsupported(
            f"native backend cannot access field `{node.name}` on `{tag}`",
            node.span,
        )

    def _emit_index(self, node: ast.IndexExpr, ctx: FnContext, w: Writer):
        recv, rtag = self._emit_expr(node.recv, ctx, w)
        if isinstance(node.index, ast.RangeExpr):
            rng = node.index
            if rtag == "str":
                return self._emit_str_slice(recv, rng, ctx, w)
            if isinstance(rtag, tuple) and rtag[0] == "vec":
                s = mangle(rtag[1])
                start = self._emit_expr(rng.start, ctx, w, expect="int")[0] if rng.start else "0"
                if rng.end is not None:
                    end = self._emit_expr(rng.end, ctx, w, expect="int")[0]
                    if rng.inclusive:
                        end = f"({end}) + 1"
                else:
                    end = f"({recv}).len"
                return f"ax_vec_slice_{s}({recv}, {start}, {end})", rtag
            raise unsupported("native slicing requires a str or Vec", node.span)
        idx, _ = self._emit_expr(node.index, ctx, w)
        if rtag == "str":
            return f"ax_str_index({recv}, {idx})", "str"
        if isinstance(rtag, tuple) and rtag[0] == "vec":
            return f"ax_vec_get_{mangle(rtag[1])}({recv}, {idx})", rtag[1]
        if isinstance(rtag, tuple) and rtag[0] == "map":
            return f"ax_map_index_{mangle(rtag[1])}({recv}, {idx})", rtag[1]
        raise unsupported(
            f"native backend cannot index a value of type `{rtag}`", node.span)

    def _emit_record(self, node: ast.RecordLit, ctx: FnContext, w: Writer):
        name = node.type_name
        if name not in self.structs:
            raise unsupported(f"native backend cannot construct `{name}`", node.span)
        self.ensure(name)
        provided = {f: v for f, v in node.fields}
        inits: list[str] = []
        for f in self.structs[name].fields:
            ftag = self.struct_fields[name][f.name]
            if f.name in provided:
                code, _ = self._emit_expr(provided[f.name], ctx, w, expect=ftag)
            elif f.default is not None:
                code, _ = self._emit_expr(f.default, ctx, w, expect=ftag)
            else:
                code = self._zero_value(ftag, ctx, w)
            inits.append((f.name, code, ftag))
        codes = self._sequence([(c, t) for _, c, t in inits], ctx, w)
        inits = [f".{cfield(n)} = {c}" for (n, _, _), c in zip(inits, codes)]
        body = ", ".join(inits)
        return f"(({name}){{{body}}})", name

    def _zero_value(self, tag, ctx: FnContext, w: Writer) -> str:
        if tag == "int":
            return "0"
        if tag == "float":
            return "0.0"
        if tag == "bool":
            return "false"
        if tag == "str":
            return 'ax_str_lit("")'
        if isinstance(tag, tuple):
            self.ensure(tag)
            if tag[0] == "opt":
                return f"ax_opt_none_{mangle(tag[1])}()"
            if tag[0] == "vec":
                return f"ax_vec_new_{mangle(tag[1])}()"
            if tag[0] == "map":
                return f"ax_map_new_{mangle(tag[1])}()"
        tmp = ctx.tmp()
        self.ensure(tag)
        w.line(f"{self.ctype(tag)} {tmp};")
        w.line(f"memset(&{tmp}, 0, sizeof({tmp}));")
        return tmp

    def _emit_list(self, node: ast.ListLit, ctx: FnContext, w: Writer, expect=None):
        if not node.elems:
            if isinstance(expect, tuple) and expect[0] == "vec":
                self.ensure(expect)
                return f"ax_vec_new_{mangle(expect[1])}()", expect
            raise unsupported(
                "an empty list literal needs a type annotation in native code",
                node.span,
            )
        elem_expect = expect[1] if isinstance(expect, tuple) and expect[0] == "vec" else None
        first_code, elem_tag = self._emit_expr(node.elems[0], ctx, w, expect=elem_expect)
        vtag = ("vec", elem_tag)
        self.ensure(vtag)
        s = mangle(elem_tag)
        tmp = ctx.tmp()
        w.line(f"{self.ctype(vtag)} {tmp} = ax_vec_new_{s}();")
        w.line(f"ax_vec_push_{s}(&{tmp}, {first_code});")
        for e in node.elems[1:]:
            code, _ = self._emit_expr(e, ctx, w, expect=elem_tag)
            w.line(f"ax_vec_push_{s}(&{tmp}, {code});")
        return tmp, vtag

    def _emit_array_repeat(self, node: ast.ArrayRepeat, ctx: FnContext, w: Writer,
                           expect=None):
        elem_expect = expect[1] if isinstance(expect, tuple) and expect[0] == "vec" else None
        value, elem_tag = self._emit_expr(node.value, ctx, w, expect=elem_expect)
        count, _ = self._emit_expr(node.count, ctx, w, expect="int")
        vtag = ("vec", elem_tag)
        self.ensure(vtag)
        s = mangle(elem_tag)
        tmp, i = ctx.tmp(), ctx.tmp()
        w.line(f"{self.ctype(vtag)} {tmp} = ax_vec_new_{s}();")
        w.line(f"for (int64_t {i} = 0; {i} < {count}; {i}++) ax_vec_push_{s}(&{tmp}, {value});")
        return tmp, vtag

    def _emit_fstr(self, node: ast.FStr, ctx: FnContext, w: Writer) -> str:
        items: list = []
        for part in node.parts:
            if isinstance(part, str):
                items.append((f"ax_str_lit({self._c_string(part)})", "str"))
            else:
                items.append((self._to_str(w, *self._emit_expr(part, ctx, w)), "str"))
        pieces = self._sequence(items, ctx, w)
        if not pieces:
            return 'ax_str_lit("")'
        acc = pieces[0]
        for p in pieces[1:]:
            acc = f"ax_str_concat({acc}, {p})"
        return acc

    def _to_str(self, w: Writer, code: str, tag) -> str:
        if tag == "str":
            return code
        if tag == "int":
            return f"ax_fmt_int({code})"
        if tag == "float":
            return f"ax_fmt_float({code})"
        if tag == "bool":
            return f"ax_fmt_bool({code})"
        show = self.ensure_show(tag)
        return f"{show}({code})"

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


# Identifiers AXON programs may use but C (or the runtime and the code this
# backend emits) reserves. User names in this set are renamed in the C output.
_C_RESERVED = {
    "auto", "char", "const", "default", "double", "extern", "float", "goto",
    "inline", "int", "long", "register", "restrict", "short", "signed",
    "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned",
    "volatile", "bool", "true", "false", "NULL", "exit", "free",
    "malloc", "realloc", "calloc", "memcpy", "memmove", "memset", "memcmp",
    "strlen", "strtod", "strtoll", "snprintf", "printf", "puts", "fputs",
    "fputc", "fgetc", "fwrite", "fflush", "fopen", "fclose", "fread", "fseek",
    "ftell", "rewind", "ferror", "getenv", "qsort", "sqrt", "pow", "floor",
    "ceil", "fabs", "fmod", "sin", "cos", "tan", "log", "exp", "llround",
    "isnan", "abs", "labs", "stdout", "stderr", "stdin", "errno", "EOF",
    "FILE", "INFINITY", "NAN", "size_t", "int8_t", "int16_t", "int32_t",
    "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t", "INT64_MAX",
    "INT64_MIN", "INT64_C", "argc", "argv", "env", "self_", "out", "acc",
}
# AXON type names also used as values (`s.parse(int)`); never renamed
_AXON_TYPE_VALUES = {
    "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16",
    "uint32", "uint64", "float32", "float64", "bool", "str", "byte", "rune",
}


def _reserved(name: str) -> bool:
    return (name in _C_RESERVED or name.startswith(("ax_", "Ax", "__ax"))) \
        and name not in _AXON_TYPE_VALUES


def cfield(name: str) -> str:
    """C member name for a struct field / enum payload field."""
    return name + "__u" if _reserved(name) else name


def _sanitize_names(module: ast.Module) -> ast.Module:
    """Return a copy of `module` with reserved value identifiers renamed.

    Only names of functions, parameters, locals, and pattern bindings change;
    field names keep their spelling (they are displayed) and are mapped to C
    member names by `cfield` instead.
    """
    module = copy.deepcopy(module)
    declared: set[str] = set()

    def collect(node) -> None:
        if isinstance(node, (list, tuple)):
            for x in node:
                collect(x)
            return
        if not isinstance(node, ast.Node):
            return
        if isinstance(node, (ast.Param, ast.BindingPattern, ast.LetStmt)):
            declared.add(node.name)
        if isinstance(node, ast.FnDef) and node.owner is None:
            declared.add(node.name)
        for f in node.__dataclass_fields__:
            if f != "span":
                collect(getattr(node, f))

    collect(module.items)

    def fix(name):
        if not isinstance(name, str) or not _reserved(name):
            return name
        if name in BUILTIN_FUNCTIONS and name not in declared:
            return name  # a call to the AXON builtin, not a user definition
        return name + "__u"

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if isinstance(node, tuple):
            for x in node:
                walk(x)
            return
        if not isinstance(node, ast.Node):
            return
        if isinstance(node, ast.NameExpr):
            node.name = fix(node.name)
        elif isinstance(node, (ast.Param, ast.BindingPattern, ast.Arg)):
            node.name = fix(node.name)
        elif isinstance(node, ast.LetStmt):
            node.name = fix(node.name)
            node.catch_name = fix(node.catch_name)
        elif isinstance(node, ast.TryExpr):
            node.catch_name = fix(node.catch_name)
        elif isinstance(node, ast.FnDef) and node.owner is None:
            node.name = fix(node.name)
        for f in node.__dataclass_fields__:
            if f != "span":
                walk(getattr(node, f))

    walk(module.items)
    return module


def generate_c(module: ast.Module) -> str:
    return CBackend(_sanitize_names(module)).generate()
