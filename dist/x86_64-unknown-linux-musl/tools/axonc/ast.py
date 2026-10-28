"""AXON abstract syntax tree.

All nodes carry a `span` for diagnostics. The tree is produced by the parser and
consumed by the semantic analyzer, the interpreter, and the C backend.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from .errors import Span


@dataclasses.dataclass
class Node:
    span: Span


# --------------------------------------------------------------------------- #
# Type expressions
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class TypeName(Node):
    name: str
    args: list["TypeExpr"] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class OptionalType(Node):
    inner: "TypeExpr"


@dataclasses.dataclass
class ResultType(Node):
    ok: "TypeExpr"
    err: Optional["TypeExpr"]


@dataclasses.dataclass
class RefType(Node):
    inner: "TypeExpr"
    mutable: bool


@dataclasses.dataclass
class PointerType(Node):
    inner: "TypeExpr"
    mutable: bool


@dataclasses.dataclass
class ArrayType(Node):
    elem: "TypeExpr"
    size: Optional["Expr"]


@dataclasses.dataclass
class SliceType(Node):
    elem: "TypeExpr"


@dataclasses.dataclass
class TupleType(Node):
    elems: list["TypeExpr"]


@dataclasses.dataclass
class FnType(Node):
    params: list["TypeExpr"]
    ret: Optional["TypeExpr"]


TypeExpr = Node


# --------------------------------------------------------------------------- #
# Top-level items
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Module(Node):
    path: object  # pathlib.Path
    uses: list["Use"]
    items: list[Node]


@dataclasses.dataclass
class Use(Node):
    module: list[str]
    names: list[str]  # empty means `use Module`


@dataclasses.dataclass
class Const(Node):
    name: str
    type: Optional[TypeExpr]
    value: "Expr"


@dataclasses.dataclass
class TypeAlias(Node):
    name: str
    aliased: TypeExpr


@dataclasses.dataclass
class Param(Node):
    name: str
    type: Optional[TypeExpr]
    default: Optional["Expr"]
    is_mut: bool
    is_self: bool
    is_variadic: bool = False


@dataclasses.dataclass
class FnDef(Node):
    name: str
    generics: list[str]
    params: list[Param]
    return_type: Optional[TypeExpr]
    body: Optional["Block"]
    attrs: list[str]
    owner: Optional[str] = None      # set for methods inside impl blocks
    self_mut: bool = False
    is_extern: bool = False


@dataclasses.dataclass
class Field(Node):
    name: str
    type: TypeExpr
    default: Optional["Expr"]


@dataclasses.dataclass
class StructDef(Node):
    name: str
    generics: list[str]
    fields: list[Field]
    attrs: list[str]


@dataclasses.dataclass
class Variant(Node):
    name: str
    # payload fields: list of (optional name, type). Positional payloads have
    # name == None.
    payload: list[tuple[Optional[str], TypeExpr]]


@dataclasses.dataclass
class EnumDef(Node):
    name: str
    generics: list[str]
    variants: list[Variant]
    attrs: list[str]


@dataclasses.dataclass
class TraitDef(Node):
    name: str
    generics: list[str]
    methods: list[FnDef]


@dataclasses.dataclass
class ImplBlock(Node):
    type_name: str
    trait_name: Optional[str]
    generics: list[str]
    methods: list[FnDef]


# --------------------------------------------------------------------------- #
# Statements
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Block(Node):
    stmts: list[Node]


@dataclasses.dataclass
class LetStmt(Node):
    name: str
    type: Optional[TypeExpr]
    value: Optional["Expr"]
    is_mut: bool
    # try/catch let form: `let x = try EXPR catch e then <block>`
    catch_name: Optional[str] = None
    catch_block: Optional[Block] = None
    # destructuring form: `let (a, b) = EXPR` (name is then "_")
    pattern: Optional["Pattern"] = None


@dataclasses.dataclass
class AssignStmt(Node):
    target: "Expr"
    op: str  # "=", "+=", ...
    value: "Expr"


@dataclasses.dataclass
class ReturnStmt(Node):
    value: Optional["Expr"]


@dataclasses.dataclass
class BreakStmt(Node):
    pass


@dataclasses.dataclass
class ContinueStmt(Node):
    pass


@dataclasses.dataclass
class DeferStmt(Node):
    expr: "Expr"


@dataclasses.dataclass
class ExprStmt(Node):
    expr: "Expr"


@dataclasses.dataclass
class IfStmt(Node):
    cond: "Expr"
    then: Block
    elifs: list[tuple["Expr", Block]]
    els: Optional[Block]


@dataclasses.dataclass
class IfLetStmt(Node):
    pattern: "Pattern"
    expr: "Expr"
    then: Block
    els: Optional[Block]


@dataclasses.dataclass
class ForStmt(Node):
    pattern: "Pattern"
    iterable: "Expr"
    body: Block


@dataclasses.dataclass
class WhileStmt(Node):
    cond: "Expr"
    body: Block


@dataclasses.dataclass
class LoopStmt(Node):
    body: Block


@dataclasses.dataclass
class MatchArm(Node):
    pattern: "Pattern"
    guard: Optional["Expr"]
    body: Block


@dataclasses.dataclass
class MatchStmt(Node):
    scrutinee: "Expr"
    arms: list[MatchArm]


@dataclasses.dataclass
class TaskStmt(Node):
    expr: "Expr"


@dataclasses.dataclass
class TaskGroupStmt(Node):
    body: Block


# --------------------------------------------------------------------------- #
# Expressions
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class IntLit(Node):
    value: int
    suffix: Optional[str]


@dataclasses.dataclass
class FloatLit(Node):
    value: float
    suffix: Optional[str]


@dataclasses.dataclass
class BoolLit(Node):
    value: bool


@dataclasses.dataclass
class NoneLit(Node):
    pass


@dataclasses.dataclass
class StrLit(Node):
    value: str


@dataclasses.dataclass
class CharLit(Node):
    value: str


@dataclasses.dataclass
class FStr(Node):
    # alternating literal text and embedded expression nodes
    parts: list[object]  # str | Expr


@dataclasses.dataclass
class NameExpr(Node):
    name: str


@dataclasses.dataclass
class SelfExpr(Node):
    pass


@dataclasses.dataclass
class Arg(Node):
    name: Optional[str]
    value: "Expr"


@dataclasses.dataclass
class CallExpr(Node):
    callee: "Expr"
    args: list[Arg]


@dataclasses.dataclass
class FieldExpr(Node):
    recv: "Expr"
    name: str


@dataclasses.dataclass
class OptFieldExpr(Node):
    recv: "Expr"
    name: str


@dataclasses.dataclass
class IndexExpr(Node):
    recv: "Expr"
    index: "Expr"


@dataclasses.dataclass
class BinaryExpr(Node):
    op: str
    left: "Expr"
    right: "Expr"


@dataclasses.dataclass
class UnaryExpr(Node):
    op: str
    operand: "Expr"


@dataclasses.dataclass
class RefExpr(Node):
    operand: "Expr"
    mutable: bool


@dataclasses.dataclass
class RangeExpr(Node):
    start: Optional["Expr"]
    end: Optional["Expr"]
    inclusive: bool


@dataclasses.dataclass
class ListLit(Node):
    elems: list["Expr"]


@dataclasses.dataclass
class ArrayRepeat(Node):
    value: "Expr"
    count: "Expr"


@dataclasses.dataclass
class TupleLit(Node):
    elems: list["Expr"]


@dataclasses.dataclass
class RecordLit(Node):
    type_name: str
    fields: list[tuple[str, "Expr"]]


@dataclasses.dataclass
class IfExpr(Node):
    cond: "Expr"
    then: "Expr"
    els: "Expr"


@dataclasses.dataclass
class MatchExpr(Node):
    scrutinee: "Expr"
    arms: list[MatchArm]


@dataclasses.dataclass
class LambdaExpr(Node):
    params: list[Param]
    body: object  # Expr (single-expression) or Block
    captures: list[str]
    ret: Optional[TypeExpr] = None  # `fn(x: int) -> int = ...`


@dataclasses.dataclass
class TryExpr(Node):
    expr: "Expr"
    # inline forms: `try x or fallback` / `try x catch e then fallback`
    or_else: Optional["Expr"] = None
    catch_name: Optional[str] = None
    catch_expr: Optional["Expr"] = None


@dataclasses.dataclass
class PipeExpr(Node):
    left: "Expr"
    right: "Expr"


@dataclasses.dataclass
class CastExpr(Node):
    expr: "Expr"
    type: TypeExpr


Expr = Node


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class WildcardPattern(Node):
    pass


@dataclasses.dataclass
class BindingPattern(Node):
    name: str


@dataclasses.dataclass
class LiteralPattern(Node):
    value: Expr


@dataclasses.dataclass
class VariantPattern(Node):
    name: str  # Some / None / Ok / Err / EnumVariant
    subpatterns: list["Pattern"]
    field_names: list[Optional[str]]


@dataclasses.dataclass
class TuplePattern(Node):
    elems: list["Pattern"]


@dataclasses.dataclass
class OrPattern(Node):
    alts: list["Pattern"]


Pattern = Node
