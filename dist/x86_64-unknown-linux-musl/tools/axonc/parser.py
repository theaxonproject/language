"""AXON recursive-descent parser.

Consumes the token stream (with NEWLINE/INDENT/DEDENT) and produces a `Module`
AST. Expression parsing uses precedence climbing following the operator table in
the language reference.
"""

from __future__ import annotations

import pathlib

from . import ast
from .errors import Diagnostic, Source, Span, parse_error
from .lexer import Token, tokenize

# Binary operator precedence; higher binds tighter.
PREC = {
    "|>": 1,
    "..": 2,
    "or": 3,
    "and": 4,
    "==": 5, "!=": 5, "<": 5, "<=": 5, ">": 5, ">=": 5, "is": 5,
    "|": 6,
    "^": 7,
    "&": 8,
    "<<": 9, ">>": 9,
    "+": 10, "-": 10,
    "*": 11, "/": 11, "%": 11,
}


def parse_number_literal(text: str) -> tuple[object, str | None, bool]:
    """Return (value, suffix, is_float)."""
    suffix = None
    body = text
    # numeric type suffix: 1_u64, 3.0_f32 — last `_<alpha...>` segment.
    if "_" in body:
        head, _, tail = body.rpartition("_")
        if tail and tail[0].isalpha():
            suffix = tail
            body = head
    clean = body.replace("_", "")
    lower = clean.lower()
    if lower.startswith("0x"):
        return int(clean, 16), suffix, False
    if lower.startswith("0b"):
        return int(clean, 2), suffix, False
    if lower.startswith("0o"):
        return int(clean, 8), suffix, False
    if "." in clean or "e" in lower:
        return float(clean), suffix, True
    return int(clean), suffix, False


def decode_string(raw: str) -> str:
    if raw.startswith('"""') and raw.endswith('"""'):
        return raw[3:-3]
    inner = raw[1:-1]
    return inner.encode("latin-1", "backslashreplace").decode("unicode_escape")


class Parser:
    def __init__(self, tokens: list[Token], source: Source) -> None:
        self.tokens = tokens
        self.source = source
        self.pos = 0

    # -- token helpers --------------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def at(self, kind: str, value: str | None = None) -> bool:
        tok = self.peek()
        if tok.kind != kind:
            return False
        return value is None or tok.value == value

    def at_value(self, value: str) -> bool:
        tok = self.peek()
        return tok.value == value and tok.kind in {"OP", "KW"}

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return tok

    def accept(self, kind: str, value: str | None = None) -> Token | None:
        if self.at(kind, value):
            return self.advance()
        return None

    def accept_value(self, value: str) -> bool:
        if self.at_value(value):
            self.advance()
            return True
        return False

    def expect(self, kind: str, value: str | None = None) -> Token:
        if not self.at(kind, value):
            want = value if value is not None else kind
            got = self.peek()
            raise parse_error(
                f"expected `{want}` but found `{got.value or got.kind}`",
                got.span,
            )
        return self.advance()

    def expect_value(self, value: str) -> Token:
        if not self.at_value(value):
            got = self.peek()
            raise parse_error(
                f"expected `{value}` but found `{got.value or got.kind}`",
                got.span,
            )
        return self.advance()

    def skip_newlines(self) -> None:
        while self.at("NEWLINE"):
            self.advance()

    def end_statement(self) -> None:
        """Terminate a statement.

        A statement normally ends with a NEWLINE, but block-form expressions
        (a multi-line `match ... do` or struct literal) consume their own
        trailing DEDENT, leaving the next token at a DEDENT/EOF boundary.
        """
        if self.accept("NEWLINE"):
            return
        if self.at("DEDENT") or self.at("EOF"):
            return
        got = self.peek()
        raise parse_error(
            f"expected end of statement but found `{got.value or got.kind}`",
            got.span,
        )

    # -- module ---------------------------------------------------------------
    def parse_module(self) -> ast.Module:
        uses: list[ast.Use] = []
        items: list[ast.Node] = []
        self.skip_newlines()
        while not self.at("EOF"):
            attrs = self.parse_attributes()
            tok = self.peek()
            if tok.kind == "KW" and tok.value == "use":
                uses.append(self.parse_use())
            elif tok.kind == "KW" and tok.value == "const":
                items.append(self.parse_const())
            elif tok.kind == "KW" and tok.value == "type":
                items.append(self.parse_type_alias())
            elif tok.kind == "KW" and tok.value == "fn":
                items.append(self.parse_fn(attrs))
            elif tok.kind == "KW" and tok.value == "struct":
                items.append(self.parse_struct(attrs))
            elif tok.kind == "KW" and tok.value == "enum":
                items.append(self.parse_enum(attrs))
            elif tok.kind == "KW" and tok.value == "trait":
                items.append(self.parse_trait())
            elif tok.kind == "KW" and tok.value == "impl":
                items.append(self.parse_impl())
            elif tok.kind == "KW" and tok.value == "extern":
                items.append(self.parse_extern_block(attrs))
            else:
                raise parse_error(
                    f"unexpected top-level token `{tok.value or tok.kind}`",
                    tok.span,
                    "top-level items are use/const/type/fn/struct/enum/trait/impl",
                )
            self.skip_newlines()
        return ast.Module(self.module_span(), self.source.path, uses, items)

    def module_span(self) -> Span:
        return Span.point(self.source, 1, 1)

    def parse_attributes(self) -> list[str]:
        attrs: list[str] = []
        while self.at("OP", "@"):
            self.advance()
            name = self.expect("NAME").value
            text = "@" + name
            if self.accept("OP", "("):
                depth = 1
                parts: list[str] = []
                while depth > 0 and not self.at("EOF"):
                    t = self.advance()
                    if t.value == "(":
                        depth += 1
                    elif t.value == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    parts.append(t.value)
                text += "(" + "".join(parts) + ")"
            attrs.append(text)
            self.skip_newlines()
        return attrs

    def parse_use(self) -> ast.Use:
        start = self.expect("KW", "use").span
        module = [self.parse_path_segment()]
        names: list[str] = []
        while self.accept("OP", "."):
            if self.accept("OP", "{"):
                while not self.at("OP", "}"):
                    names.append(self.advance().value)
                    if not self.accept("OP", ","):
                        break
                self.expect("OP", "}")
                break
            module.append(self.parse_path_segment())
        end = self.peek().span
        self.expect("NEWLINE")
        return ast.Use(start.to(end), module, names)

    def parse_path_segment(self) -> str:
        tok = self.advance()
        return tok.value

    def parse_const(self) -> ast.Const:
        start = self.expect("KW", "const").span
        name = self.advance().value
        type_ann = None
        if self.accept("OP", ":"):
            type_ann = self.parse_type()
        self.expect("OP", "=")
        value = self.parse_expr()
        self.expect("NEWLINE")
        return ast.Const(start, name, type_ann, value)

    def parse_type_alias(self) -> ast.TypeAlias:
        start = self.expect("KW", "type").span
        name = self.advance().value
        self.expect("OP", "=")
        aliased = self.parse_type()
        self.expect("NEWLINE")
        return ast.TypeAlias(start, name, aliased)

    # -- functions ------------------------------------------------------------
    def parse_fn(self, attrs: list[str], owner: str | None = None) -> ast.FnDef:
        start = self.expect("KW", "fn").span
        name = self.advance().value
        generics = self.parse_optional_generics()
        self.expect("OP", "(")
        params, self_mut = self.parse_params()
        return_type = None
        if self.accept("OP", "->"):
            return_type = self.parse_type()
        body: ast.Block | None = None
        if self.accept("OP", "="):
            expr = self.parse_expr()
            ret = ast.ReturnStmt(expr.span, expr)
            body = ast.Block(expr.span, [ret])
            self.expect("NEWLINE")
        elif self.at("NEWLINE"):
            self.expect("NEWLINE")
            if self.at("INDENT"):
                body = self.parse_block()
            else:
                body = ast.Block(start, [])
        else:
            # extern / signature only
            self.accept("NEWLINE")
        return ast.FnDef(
            start, name, generics, params, return_type, body, attrs,
            owner=owner, self_mut=self_mut,
        )

    def parse_optional_generics(self) -> list[str]:
        generics: list[str] = []
        # AXON writes generics as `fn name(T, U)(params)` is not used; the
        # examples use `Type(T)` style. We accept an optional `[T, U]` list.
        if self.at("OP", "["):
            self.advance()
            while not self.at("OP", "]"):
                generics.append(self.advance().value)
                if not self.accept("OP", ","):
                    break
            self.expect("OP", "]")
        return generics

    def parse_params(self) -> tuple[list[ast.Param], bool]:
        params: list[ast.Param] = []
        self_mut = False
        while not self.at("OP", ")"):
            span = self.peek().span
            is_mut = bool(self.accept("KW", "mut"))
            if self.at("KW", "self"):
                self.advance()
                params.append(ast.Param(span, "self", None, None, is_mut, True))
                self_mut = is_mut
                if not self.accept("OP", ","):
                    break
                continue
            if self.accept("OP", "."):  # variadic `...`
                self.expect("OP", ".")
                self.expect("OP", ".")
                params.append(ast.Param(span, "args", None, None, False, False, True))
                self.accept("OP", ",")
                continue
            name = self.advance().value
            type_ann = None
            if self.accept("OP", ":"):
                type_ann = self.parse_type()
            default = None
            if self.accept("OP", "="):
                default = self.parse_expr()
            params.append(ast.Param(span, name, type_ann, default, is_mut, False))
            if not self.accept("OP", ","):
                break
        self.expect("OP", ")")
        return params, self_mut

    # -- struct / enum / trait / impl ----------------------------------------
    def parse_struct(self, attrs: list[str]) -> ast.StructDef:
        start = self.expect("KW", "struct").span
        name = self.advance().value
        generics = self.parse_type_param_list()
        fields: list[ast.Field] = []
        self.expect("NEWLINE")
        if self.at("INDENT"):
            self.advance()
            self.skip_newlines()
            while not self.at("DEDENT") and not self.at("EOF"):
                self.parse_attributes()
                fspan = self.peek().span
                fname = self.advance().value
                self.expect("OP", ":")
                ftype = self.parse_type()
                default = None
                if self.accept("OP", "="):
                    default = self.parse_expr()
                fields.append(ast.Field(fspan, fname, ftype, default))
                self.skip_newlines()
            self.expect("DEDENT")
        return ast.StructDef(start, name, generics, fields, attrs)

    def parse_type_param_list(self) -> list[str]:
        generics: list[str] = []
        if self.accept("OP", "("):
            while not self.at("OP", ")"):
                generics.append(self.advance().value)
                if not self.accept("OP", ","):
                    break
            self.expect("OP", ")")
        return generics

    def parse_enum(self, attrs: list[str]) -> ast.EnumDef:
        start = self.expect("KW", "enum").span
        name = self.advance().value
        generics = self.parse_type_param_list()
        variants: list[ast.Variant] = []
        self.expect("NEWLINE")
        if self.at("INDENT"):
            self.advance()
            self.skip_newlines()
            while not self.at("DEDENT") and not self.at("EOF"):
                vspan = self.peek().span
                vname = self.advance().value
                payload: list[tuple[str | None, ast.TypeExpr]] = []
                if self.accept("OP", "("):
                    while not self.at("OP", ")"):
                        pname = None
                        if self.peek().kind == "NAME" and self.peek(1).value == ":":
                            pname = self.advance().value
                            self.advance()  # :
                        ptype = self.parse_type()
                        payload.append((pname, ptype))
                        if not self.accept("OP", ","):
                            break
                    self.expect("OP", ")")
                variants.append(ast.Variant(vspan, vname, payload))
                self.skip_newlines()
            self.expect("DEDENT")
        return ast.EnumDef(start, name, generics, variants, attrs)

    def parse_trait(self) -> ast.TraitDef:
        start = self.expect("KW", "trait").span
        name = self.advance().value
        generics = self.parse_type_param_list()
        methods: list[ast.FnDef] = []
        self.expect("NEWLINE")
        if self.at("INDENT"):
            self.advance()
            self.skip_newlines()
            while not self.at("DEDENT") and not self.at("EOF"):
                attrs = self.parse_attributes()
                methods.append(self.parse_fn(attrs, owner=name))
                self.skip_newlines()
            self.expect("DEDENT")
        return ast.TraitDef(start, name, generics, methods)

    def parse_impl(self) -> ast.ImplBlock:
        start = self.expect("KW", "impl").span
        generics = self.parse_type_param_list() if self.at("OP", "(") else []
        type_name = self.advance().value
        # optional generic args on the type: impl Foo(T)
        if self.at("OP", "("):
            self.parse_type_param_list()
        trait_name = None
        if self.accept("KW", "as"):
            trait_name = self.advance().value
            while self.accept("OP", "."):
                trait_name = self.advance().value
        methods: list[ast.FnDef] = []
        self.expect("NEWLINE")
        if self.at("INDENT"):
            self.advance()
            self.skip_newlines()
            while not self.at("DEDENT") and not self.at("EOF"):
                attrs = self.parse_attributes()
                methods.append(self.parse_fn(attrs, owner=type_name))
                self.skip_newlines()
            self.expect("DEDENT")
        return ast.ImplBlock(start, type_name, trait_name, generics, methods)

    def parse_extern_block(self, attrs: list[str]) -> ast.Node:
        start = self.expect("KW", "extern").span
        # skip the extern block body; FFI is recognised but not yet lowered.
        while not self.at("NEWLINE") and not self.at("EOF"):
            self.advance()
        self.accept("NEWLINE")
        if self.at("INDENT"):
            depth = 0
            self.advance()
            depth = 1
            while depth > 0 and not self.at("EOF"):
                if self.at("INDENT"):
                    depth += 1
                elif self.at("DEDENT"):
                    depth -= 1
                self.advance()
        return ast.StructDef(start, "__extern__", [], [], attrs)

    # -- blocks & statements --------------------------------------------------
    def parse_block(self) -> ast.Block:
        start = self.peek().span
        self.expect("INDENT")
        stmts: list[ast.Node] = []
        self.skip_newlines()
        while not self.at("DEDENT") and not self.at("EOF"):
            stmts.append(self.parse_statement())
            self.skip_newlines()
        self.expect("DEDENT")
        return ast.Block(start, stmts)

    def parse_statement(self) -> ast.Node:
        tok = self.peek()
        if tok.kind == "OP" and tok.value == "@":
            self.parse_attributes()
            tok = self.peek()
        if tok.kind == "KW":
            if tok.value == "let":
                return self.parse_let()
            if tok.value == "return":
                return self.parse_return()
            if tok.value == "break":
                self.advance(); self.expect("NEWLINE")
                return ast.BreakStmt(tok.span)
            if tok.value == "continue":
                self.advance(); self.expect("NEWLINE")
                return ast.ContinueStmt(tok.span)
            if tok.value == "defer":
                self.advance()
                expr = self.parse_expr(); self.end_statement()
                return ast.DeferStmt(tok.span, expr)
            if tok.value == "if":
                return self.parse_if_statement()
            if tok.value == "for":
                return self.parse_for()
            if tok.value == "while":
                return self.parse_while()
            if tok.value == "loop":
                return self.parse_loop()
            if tok.value == "match":
                return self.parse_match_statement()
            if tok.value == "task_group":
                return self.parse_task_group()
            if tok.value == "task":
                self.advance(); self.expect("KW", "do")
                expr = self.parse_expr(); self.end_statement()
                return ast.TaskStmt(tok.span, expr)
        # assignment or expression statement
        return self.parse_assign_or_expr()

    def parse_let(self) -> ast.Node:
        start = self.expect("KW", "let").span
        is_mut = bool(self.accept("KW", "mut"))
        # destructuring `let Some(x) = ...` is parsed as if-let only; here a name.
        name = self.advance().value
        type_ann = None
        if self.accept("OP", ":"):
            type_ann = self.parse_type()
        value = None
        catch_name = None
        catch_block = None
        if self.accept("OP", "="):
            if self.at("KW", "try"):
                # could be `try EXPR catch e then` block form
                save = self.pos
                self.advance()  # try
                inner = self.parse_expr()
                if self.at("KW", "catch"):
                    self.advance()
                    catch_name = self.advance().value
                    self.expect("KW", "then")
                    self.expect("NEWLINE")
                    catch_block = self.parse_block()
                    value = ast.TryExpr(start, inner)
                    return ast.LetStmt(start, name, type_ann, value, is_mut, catch_name, catch_block)
                else:
                    value = ast.TryExpr(start, inner)
                    self.maybe_inline_try_tail(value)
                    self.end_statement()
                    return ast.LetStmt(start, name, type_ann, value, is_mut)
            value = self.parse_expr()
        if not isinstance(value, ast.MatchExpr):
            self.end_statement()
        return ast.LetStmt(start, name, type_ann, value, is_mut)

    def maybe_inline_try_tail(self, try_expr: ast.TryExpr) -> None:
        if self.accept("KW", "or"):
            try_expr.or_else = self.parse_expr()
        elif self.at("KW", "catch"):
            self.advance()
            try_expr.catch_name = self.advance().value
            self.expect("KW", "then")
            try_expr.catch_expr = self.parse_expr()

    def parse_return(self) -> ast.ReturnStmt:
        start = self.expect("KW", "return").span
        value = None
        if not self.at("NEWLINE") and not self.at("DEDENT") and not self.at("EOF"):
            value = self.parse_expr()
        self.end_statement()
        return ast.ReturnStmt(start, value)

    def parse_if_statement(self) -> ast.Node:
        start = self.expect("KW", "if").span
        if self.at("KW", "let"):
            self.advance()
            pattern = self.parse_pattern()
            self.expect("OP", "=")
            expr = self.parse_expr()
            self.expect("KW", "then")
            self.expect("NEWLINE")
            then = self.parse_block()
            els = self.parse_else_block()
            return ast.IfLetStmt(start, pattern, expr, then, els)
        cond = self.parse_expr()
        self.expect("KW", "then")
        if not self.at("NEWLINE"):
            # inline if-expression used as a statement
            then_expr = self.parse_expr()
            els_expr = None
            if self.accept("KW", "else"):
                els_expr = self.parse_expr()
            self.expect("NEWLINE")
            if els_expr is None:
                els_expr = ast.NoneLit(start)
            return ast.ExprStmt(start, ast.IfExpr(start, cond, then_expr, els_expr))
        self.expect("NEWLINE")
        then = self.parse_block()
        elifs: list[tuple[ast.Expr, ast.Block]] = []
        els = None
        while self.at("KW", "else"):
            # peek to distinguish `else if` from `else`
            if self.peek(1).kind == "KW" and self.peek(1).value == "if":
                self.advance(); self.advance()  # else if
                econd = self.parse_expr()
                self.expect("KW", "then")
                self.expect("NEWLINE")
                eblock = self.parse_block()
                elifs.append((econd, eblock))
            else:
                self.advance()  # else
                self.expect("NEWLINE")
                els = self.parse_block()
                break
        return ast.IfStmt(start, cond, then, elifs, els)

    def parse_else_block(self) -> ast.Block | None:
        if self.at("KW", "else"):
            self.advance()
            self.expect("NEWLINE")
            return self.parse_block()
        return None

    def parse_for(self) -> ast.ForStmt:
        start = self.expect("KW", "for").span
        pattern = self.parse_pattern()
        self.expect("KW", "in")
        iterable = self.parse_expr()
        self.expect("KW", "do")
        self.expect("NEWLINE")
        body = self.parse_block()
        return ast.ForStmt(start, pattern, iterable, body)

    def parse_while(self) -> ast.WhileStmt:
        start = self.expect("KW", "while").span
        cond = self.parse_expr()
        self.expect("KW", "do")
        self.expect("NEWLINE")
        body = self.parse_block()
        return ast.WhileStmt(start, cond, body)

    def parse_loop(self) -> ast.LoopStmt:
        start = self.expect("KW", "loop").span
        self.expect("KW", "do")
        self.expect("NEWLINE")
        body = self.parse_block()
        return ast.LoopStmt(start, body)

    def parse_task_group(self) -> ast.TaskGroupStmt:
        start = self.expect("KW", "task_group").span
        self.expect("KW", "do")
        self.expect("NEWLINE")
        body = self.parse_block()
        return ast.TaskGroupStmt(start, body)

    def parse_match_statement(self) -> ast.Node:
        # The block-form `match ... do <arms>` is terminated by its own DEDENT,
        # so no statement terminator follows.
        expr = self.parse_match_expr()
        return ast.ExprStmt(expr.span, expr)

    def parse_match_expr(self) -> ast.MatchExpr:
        start = self.expect("KW", "match").span
        scrutinee = self.parse_expr()
        self.expect("KW", "do")
        self.expect("NEWLINE")
        self.expect("INDENT")
        arms: list[ast.MatchArm] = []
        self.skip_newlines()
        while not self.at("DEDENT") and not self.at("EOF"):
            arms.append(self.parse_match_arm())
            self.skip_newlines()
        self.expect("DEDENT")
        return ast.MatchExpr(start, scrutinee, arms)

    def parse_match_arm(self) -> ast.MatchArm:
        start = self.expect("KW", "case").span
        pattern = self.parse_pattern()
        guard = None
        if self.accept("KW", "if"):
            guard = self.parse_expr()
        self.expect("KW", "then")
        if self.at("NEWLINE"):
            self.advance()
            body = self.parse_block()
        else:
            stmt = self.parse_inline_arm_body()
            body = ast.Block(start, [stmt])
        return ast.MatchArm(start, pattern, guard, body)

    def parse_inline_arm_body(self) -> ast.Node:
        # An inline arm body may be `return X`, `break`, or an expression.
        if self.at("KW", "return"):
            sp = self.advance().span
            val = None if self.at("NEWLINE") else self.parse_expr()
            return ast.ReturnStmt(sp, val)
        if self.at("KW", "break"):
            return ast.BreakStmt(self.advance().span)
        if self.at("KW", "continue"):
            return ast.ContinueStmt(self.advance().span)
        expr = self.parse_expr()
        return ast.ExprStmt(expr.span, expr)

    def parse_assign_or_expr(self) -> ast.Node:
        expr = self.parse_expr()
        if self.peek().kind == "OP" and self.peek().value in {
            "=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=",
        }:
            op = self.advance().value
            value = self.parse_expr()
            if not isinstance(value, ast.MatchExpr):
                self.end_statement()
            return ast.AssignStmt(expr.span, expr, op, value)
        if not isinstance(expr, ast.MatchExpr):
            self.end_statement()
        return ast.ExprStmt(expr.span, expr)

    # -- expressions ----------------------------------------------------------
    def parse_expr(self) -> ast.Expr:
        if self.at("KW", "try"):
            start = self.advance().span
            inner = self.parse_binary(0)
            node = ast.TryExpr(start, inner)
            self.maybe_inline_try_tail(node)
            return node
        return self.parse_lambda_or_binary()

    def parse_lambda_or_binary(self) -> ast.Expr:
        # `NAME => body` or `NAME, NAME, ... => body` or `fn(...) = body`
        if self.at("KW", "fn") and self.peek(1).value == "(":
            return self.parse_fn_lambda()
        if self.peek().kind == "NAME" and self._lookahead_lambda():
            return self.parse_bare_lambda()
        return self.parse_binary(0)

    def _lookahead_lambda(self) -> bool:
        i = self.pos
        if self.tokens[i].kind != "NAME":
            return False
        i += 1
        while self.tokens[i].value == "," and self.tokens[i + 1].kind == "NAME":
            i += 2
        return self.tokens[i].value == "=>"

    def parse_bare_lambda(self) -> ast.Expr:
        start = self.peek().span
        params: list[ast.Param] = []
        while True:
            name_tok = self.expect("NAME")
            params.append(ast.Param(name_tok.span, name_tok.value, None, None, False, False))
            if not self.accept("OP", ","):
                break
        self.expect_value("=>")
        body = self.parse_expr()
        return ast.LambdaExpr(start, params, body, [])

    def parse_fn_lambda(self) -> ast.Expr:
        start = self.expect("KW", "fn").span
        self.expect("OP", "(")
        params, _ = self.parse_params()
        if self.accept("OP", "->"):
            self.parse_type()
        self.expect("OP", "=")
        body = self.parse_expr()
        return ast.LambdaExpr(start, params, body, [])

    def parse_binary(self, min_prec: int) -> ast.Expr:
        left = self.parse_unary()
        while True:
            tok = self.peek()
            op = tok.value
            if tok.kind == "KW" and op == "is":
                pass
            elif tok.kind != "OP":
                break
            prec = PREC.get(op)
            if prec is None or prec < min_prec:
                break
            self.advance()
            if op == "..":
                # range: optional right operand
                if self.peek().value in {")", "]", ",", "}"} or self.at("NEWLINE"):
                    left = ast.RangeExpr(tok.span, left, None, False)
                    continue
                right = self.parse_binary(prec + 1)
                left = ast.RangeExpr(tok.span, left, right, False)
                continue
            if op == "|>":
                right = self.parse_binary(prec + 1)
                left = ast.PipeExpr(tok.span, left, right)
                continue
            right = self.parse_binary(prec + 1)
            left = ast.BinaryExpr(tok.span, op, left, right)
        return left

    def parse_unary(self) -> ast.Expr:
        tok = self.peek()
        if tok.kind == "KW" and tok.value == "not":
            self.advance()
            return ast.UnaryExpr(tok.span, "not", self.parse_unary())
        if tok.kind == "KW" and tok.value == "ref":
            self.advance()
            return ast.RefExpr(tok.span, self.parse_unary(), False)
        if tok.kind == "KW" and tok.value == "mut":
            # `mut ref expr`
            self.advance()
            self.expect("KW", "ref")
            return ast.RefExpr(tok.span, self.parse_unary(), True)
        if tok.kind == "OP" and tok.value in {"-", "+", "~", "*"}:
            self.advance()
            return ast.UnaryExpr(tok.span, tok.value, self.parse_unary())
        if tok.kind == "OP" and tok.value == "&":
            self.advance()
            mutable = bool(self.accept("KW", "mut"))
            return ast.RefExpr(tok.span, self.parse_unary(), mutable)
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Expr:
        expr = self.parse_primary()
        while True:
            tok = self.peek()
            if tok.kind == "OP" and tok.value == "(":
                self.advance()
                args = self.parse_call_args()
                expr = ast.CallExpr(tok.span, expr, args)
                continue
            if tok.kind == "OP" and tok.value == ".":
                self.advance()
                name = self.advance().value
                expr = ast.FieldExpr(tok.span, expr, name)
                continue
            if tok.kind == "OP" and tok.value == "?.":
                self.advance()
                name = self.advance().value
                expr = ast.OptFieldExpr(tok.span, expr, name)
                continue
            if tok.kind == "OP" and tok.value == "?":
                # postfix `?` try operator
                self.advance()
                expr = ast.TryExpr(tok.span, expr)
                continue
            if tok.kind == "OP" and tok.value == "[":
                self.advance()
                index = self.parse_expr()
                self.expect("OP", "]")
                expr = ast.IndexExpr(tok.span, expr, index)
                continue
            if tok.kind == "KW" and tok.value == "as":
                self.advance()
                ty = self.parse_type()
                expr = ast.CastExpr(tok.span, expr, ty)
                continue
            break
        return expr

    def parse_call_args(self) -> list[ast.Arg]:
        args: list[ast.Arg] = []
        if self.accept("OP", ")"):
            return args
        while True:
            span = self.peek().span
            name = None
            if self.peek().kind == "NAME" and self.peek(1).value == ":" and self.peek(2).value != ":":
                name = self.advance().value
                self.advance()  # :
            value = self.parse_expr()
            args.append(ast.Arg(span, name, value))
            if not self.accept("OP", ","):
                break
        self.expect("OP", ")")
        return args

    def parse_primary(self) -> ast.Expr:
        tok = self.peek()
        if tok.kind == "INT":
            self.advance()
            value, suffix, is_float = parse_number_literal(tok.value)
            if is_float:
                return ast.FloatLit(tok.span, value, suffix)
            return ast.IntLit(tok.span, value, suffix)
        if tok.kind == "FLOAT":
            self.advance()
            value, suffix, _ = parse_number_literal(tok.value)
            return ast.FloatLit(tok.span, float(value), suffix)
        if tok.kind == "STRING":
            self.advance()
            return ast.StrLit(tok.span, decode_string(tok.value))
        if tok.kind == "RAWSTRING":
            self.advance()
            return ast.StrLit(tok.span, tok.value[2:-1])
        if tok.kind == "CHAR":
            self.advance()
            return ast.CharLit(tok.span, decode_string(tok.value))
        if tok.kind == "FSTRING":
            self.advance()
            return self.parse_fstring(tok)
        if tok.kind == "KW":
            if tok.value == "true":
                self.advance(); return ast.BoolLit(tok.span, True)
            if tok.value == "false":
                self.advance(); return ast.BoolLit(tok.span, False)
            if tok.value in {"none", "None"}:
                self.advance(); return ast.NoneLit(tok.span)
            if tok.value == "self":
                self.advance(); return ast.SelfExpr(tok.span)
            if tok.value == "if":
                return self.parse_if_expr()
            if tok.value == "match":
                return self.parse_match_expr()
            if tok.value in {"Some", "Ok", "Err"}:
                self.advance()
                # constructor call e.g. Some(x)
                return ast.NameExpr(tok.span, tok.value)
        if tok.kind == "NAME":
            self.advance()
            # struct literal: NAME { field: value, ... }
            if self.at("OP", "{"):
                return self.parse_record(tok)
            return ast.NameExpr(tok.span, tok.value)
        if tok.kind == "OP" and tok.value == "(":
            return self.parse_paren_or_tuple()
        if tok.kind == "OP" and tok.value == "[":
            return self.parse_list()
        raise parse_error(
            f"unexpected token `{tok.value or tok.kind}` in expression",
            tok.span,
        )

    def parse_record(self, name_tok: Token) -> ast.RecordLit:
        self.expect("OP", "{")
        fields: list[tuple[str, ast.Expr]] = []
        self.skip_newlines()
        while not self.at("OP", "}"):
            fname = self.advance().value
            self.expect("OP", ":")
            value = self.parse_expr()
            fields.append((fname, value))
            self.skip_newlines()
            if not self.accept("OP", ","):
                self.skip_newlines()
                break
            self.skip_newlines()
        self.expect("OP", "}")
        return ast.RecordLit(name_tok.span, name_tok.value, fields)

    def parse_paren_or_tuple(self) -> ast.Expr:
        start = self.expect("OP", "(").span
        if self.accept("OP", ")"):
            return ast.TupleLit(start, [])
        first = self.parse_expr()
        if self.at("OP", ","):
            elems = [first]
            while self.accept("OP", ","):
                if self.at("OP", ")"):
                    break
                elems.append(self.parse_expr())
            self.expect("OP", ")")
            return ast.TupleLit(start, elems)
        self.expect("OP", ")")
        return first

    def parse_list(self) -> ast.Expr:
        start = self.expect("OP", "[").span
        if self.accept("OP", "]"):
            return ast.ListLit(start, [])
        first = self.parse_expr()
        if self.accept("OP", ";"):
            count = self.parse_expr()
            self.expect("OP", "]")
            return ast.ArrayRepeat(start, first, count)
        elems = [first]
        while self.accept("OP", ","):
            if self.at("OP", "]"):
                break
            elems.append(self.parse_expr())
        self.expect("OP", "]")
        return ast.ListLit(start, elems)

    def parse_if_expr(self) -> ast.Expr:
        start = self.expect("KW", "if").span
        cond = self.parse_expr()
        self.expect("KW", "then")
        then = self.parse_expr()
        self.expect("KW", "else")
        els = self.parse_expr()
        return ast.IfExpr(start, cond, then, els)

    def parse_fstring(self, tok: Token) -> ast.FStr:
        raw = tok.value
        if raw.startswith('f"""'):
            inner = raw[4:-3]
        else:
            inner = raw[2:-1]
        parts: list[object] = []
        buf = ""
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "{" and i + 1 < len(inner) and inner[i + 1] == "{":
                buf += "{"; i += 2; continue
            if ch == "}" and i + 1 < len(inner) and inner[i + 1] == "}":
                buf += "}"; i += 2; continue
            if ch == "{":
                if buf:
                    parts.append(decode_string('"' + buf + '"'))
                    buf = ""
                j = i + 1
                depth = 1
                while j < len(inner) and depth > 0:
                    if inner[j] == "{":
                        depth += 1
                    elif inner[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                expr_src = inner[i + 1:j]
                parts.append(parse_embedded_expr(expr_src, tok.span))
                i = j + 1
                continue
            buf += ch
            i += 1
        if buf:
            parts.append(decode_string('"' + buf + '"'))
        return ast.FStr(tok.span, parts)

    # -- types ----------------------------------------------------------------
    def parse_type(self) -> ast.TypeExpr:
        ty = self.parse_type_core()
        # postfix `?` optional and `!E` result
        while True:
            if self.at("OP", "?"):
                sp = self.advance().span
                ty = ast.OptionalType(sp, ty)
                continue
            if self.at("OP", "!"):
                sp = self.advance().span
                err = None
                if self.peek().kind in {"NAME", "KW"} and self.peek().value not in {"then", "do"}:
                    err = self.parse_type_core()
                ty = ast.ResultType(sp, ty, err)
                continue
            break
        return ty

    def parse_type_core(self) -> ast.TypeExpr:
        tok = self.peek()
        if tok.kind == "KW" and tok.value == "ref":
            self.advance()
            mutable = bool(self.accept("KW", "mut"))
            return ast.RefType(tok.span, self.parse_type_core(), mutable)
        if tok.kind == "KW" and tok.value == "mut":
            self.advance()
            self.expect("KW", "ref")
            return ast.RefType(tok.span, self.parse_type_core(), True)
        if tok.kind == "OP" and tok.value == "&":
            self.advance()
            mutable = bool(self.accept("KW", "mut"))
            return ast.RefType(tok.span, self.parse_type_core(), mutable)
        if tok.kind == "OP" and tok.value == "*":
            self.advance()
            mutable = bool(self.accept("KW", "mut"))
            return ast.PointerType(tok.span, self.parse_type_core(), mutable)
        if tok.kind == "KW" and tok.value == "dyn":
            self.advance()
            return self.parse_type_core()
        if tok.kind == "KW" and tok.value == "fn":
            self.advance()
            self.expect("OP", "(")
            params: list[ast.TypeExpr] = []
            while not self.at("OP", ")"):
                params.append(self.parse_type())
                if not self.accept("OP", ","):
                    break
            self.expect("OP", ")")
            ret = None
            if self.accept("OP", "->"):
                ret = self.parse_type()
            return ast.FnType(tok.span, params, ret)
        if tok.kind == "OP" and tok.value == "(":
            self.advance()
            elems: list[ast.TypeExpr] = []
            while not self.at("OP", ")"):
                elems.append(self.parse_type())
                if not self.accept("OP", ","):
                    break
            self.expect("OP", ")")
            return ast.TupleType(tok.span, elems)
        if tok.kind == "OP" and tok.value == "[":
            self.advance()
            elem = self.parse_type()
            if self.accept("OP", ";"):
                size = self.parse_expr()
                self.expect("OP", "]")
                return ast.ArrayType(tok.span, elem, size)
            self.expect("OP", "]")
            return ast.SliceType(tok.span, elem)
        # named type, possibly generic: Name(T, U) or Name
        name = self.advance().value
        args: list[ast.TypeExpr] = []
        if self.at("OP", "("):
            self.advance()
            while not self.at("OP", ")"):
                args.append(self.parse_type())
                if not self.accept("OP", ","):
                    break
            self.expect("OP", ")")
        # dotted module-qualified type: keep last segment
        while self.at("OP", ".") and self.peek(1).kind in {"NAME"}:
            self.advance()
            name = self.advance().value
        return ast.TypeName(tok.span, name, args)

    # -- patterns -------------------------------------------------------------
    def parse_pattern(self) -> ast.Pattern:
        pat = self.parse_pattern_atom()
        if self.at("OP", "|"):
            alts = [pat]
            while self.accept("OP", "|"):
                alts.append(self.parse_pattern_atom())
            return ast.OrPattern(pat.span, alts)
        return pat

    def parse_pattern_atom(self) -> ast.Pattern:
        tok = self.peek()
        if tok.kind == "OP" and tok.value == "(":
            self.advance()
            elems: list[ast.Pattern] = []
            while not self.at("OP", ")"):
                elems.append(self.parse_pattern())
                if not self.accept("OP", ","):
                    break
            self.expect("OP", ")")
            if len(elems) == 1:
                return elems[0]
            return ast.TuplePattern(tok.span, elems)
        if tok.kind == "OP" and tok.value == "*":
            # `*x` deref binding in patterns -> treat as binding
            self.advance()
            name = self.advance().value
            return ast.BindingPattern(tok.span, name)
        if tok.kind == "KW" and tok.value in {"none", "None"}:
            self.advance()
            return ast.VariantPattern(tok.span, "None", [], [])
        if tok.kind == "KW" and tok.value in {"Some", "Ok", "Err"}:
            name = self.advance().value
            subs, fnames = self.parse_variant_payload()
            return ast.VariantPattern(tok.span, name, subs, fnames)
        if tok.kind == "NAME" and tok.value == "_":
            self.advance()
            return ast.WildcardPattern(tok.span)
        if tok.kind == "NAME" and tok.value[0].isupper():
            # enum variant pattern, possibly with payload
            name = self.advance().value
            if self.at("OP", "("):
                subs, fnames = self.parse_variant_payload()
                return ast.VariantPattern(tok.span, name, subs, fnames)
            return ast.VariantPattern(tok.span, name, [], [])
        if tok.kind == "NAME":
            self.advance()
            return ast.BindingPattern(tok.span, tok.value)
        # literal pattern
        expr = self.parse_unary()
        return ast.LiteralPattern(tok.span, expr)

    def parse_variant_payload(self) -> tuple[list[ast.Pattern], list[str | None]]:
        subs: list[ast.Pattern] = []
        fnames: list[str | None] = []
        if not self.accept("OP", "("):
            return subs, fnames
        while not self.at("OP", ")"):
            fname = None
            if self.peek().kind == "NAME" and self.peek(1).value == ":":
                fname = self.advance().value
                self.advance()
            subs.append(self.parse_pattern())
            fnames.append(fname)
            if not self.accept("OP", ","):
                break
        self.expect("OP", ")")
        return subs, fnames


def parse_embedded_expr(fragment: str, span: Span) -> ast.Expr:
    sub_source = Source.from_text(str(span.source.path), fragment)
    tokens = tokenize(sub_source)
    parser = Parser(tokens, sub_source)
    parser.skip_newlines()
    expr = parser.parse_expr()
    return expr


def parse_source(source: Source) -> ast.Module:
    tokens = tokenize(source)
    return Parser(tokens, source).parse_module()


def parse_path(path: pathlib.Path) -> ast.Module:
    return parse_source(Source.from_path(path))
