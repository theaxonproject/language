"""AXON lexer.

A character-level scanner that turns source text into a flat token stream with
explicit NEWLINE / INDENT / DEDENT tokens (Python-style significant
indentation, one level == four spaces) and precise source spans.

Continuations: a physical newline does not end a statement when it occurs inside
`()`, `[]`, or `{}`, when the line ends with a backslash, or when the next
logical line begins with `.` (method-chain continuation).
"""

from __future__ import annotations

import dataclasses

from .errors import Diagnostic, Source, Span, indent_error, lex_error

KEYWORDS = {
    "and", "as", "break", "capture", "case", "catch", "const", "continue",
    "defer", "do", "dyn", "else", "enum", "extern", "false", "fn", "for", "if",
    "impl", "in", "is", "let", "loop", "match", "mut", "never", "none", "not",
    "or", "ref", "return", "select", "self", "struct", "task", "task_group",
    "then", "trait", "true", "try", "type", "unsafe", "use", "void", "while",
    "where", "with", "Some", "None", "Ok", "Err",
}

# Multi-character operators, longest first so the scanner is greedy.
OPERATORS = [
    "<<=", ">>=",
    "..=", "?.", "==", "!=", "<=", ">=", "<<", ">>", "|>", "..", "->", "=>",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "+", "-", "*", "/", "%", "<", ">", "=", "&", "|", "^", "~", "!",
    ".", ",", ":", ";", "(", ")", "{", "}", "[", "]", "?", "@",
]

OPEN_BRACKETS = {"(": ")", "[": "]", "{": "}"}
CLOSE_BRACKETS = {")", "]", "}"}


@dataclasses.dataclass(frozen=True)
class Token:
    kind: str  # NAME, KW, INT, FLOAT, STRING, RAWSTRING, FSTRING, CHAR, OP,
    #            NEWLINE, INDENT, DEDENT, EOF
    value: str
    span: Span

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind}, {self.value!r})"


class Lexer:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.text = source.text
        self.n = len(self.text)
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []
        self.indent_stack = [0]
        self.paren_depth = 0
        self.at_line_start = True

    # -- low level cursor helpers --------------------------------------------
    def _peek(self, offset: int = 0) -> str:
        idx = self.pos + offset
        return self.text[idx] if idx < self.n else ""

    def _advance(self) -> str:
        ch = self.text[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _span_here(self) -> Span:
        return Span.point(self.source, self.line, self.col)

    def _emit(self, kind: str, value: str, start_line: int, start_col: int) -> None:
        span = Span(self.source, start_line, start_col, self.line, self.col)
        self.tokens.append(Token(kind, value, span))

    # -- main driver ----------------------------------------------------------
    def lex(self) -> list[Token]:
        while self.pos < self.n:
            if self.at_line_start and self.paren_depth == 0:
                if self._handle_line_start():
                    continue
            self._scan_token()
        # finish: trailing NEWLINE, then unwind indentation, then EOF.
        if self.tokens and self.tokens[-1].kind not in {"NEWLINE", "INDENT", "DEDENT"}:
            self._emit("NEWLINE", "", self.line, self.col)
        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            self._emit("DEDENT", "", self.line, self.col)
        self._emit("EOF", "", self.line, self.col)
        return self.tokens

    def _handle_line_start(self) -> bool:
        """Process indentation at the start of a logical line.

        Returns True if the caller should `continue` the main loop (the line was
        blank / a comment / a continuation and produced no real token).
        """
        start = self.pos
        indent = 0
        while self._peek() == " ":
            self._advance()
            indent += 1
        if self._peek() == "\t":
            raise indent_error(
                "tabs are not allowed in indentation",
                self._span_here(),
                "use four spaces per indentation level",
            )
        ch = self._peek()
        # Blank line or comment-only line: ignore for indentation purposes.
        if ch in {"", "\n"}:
            if ch == "\n":
                self._advance()
            return True
        if ch == "#":
            self._skip_comment()
            return True
        # Method-chain continuation: a line beginning with `.x` continues the
        # previous logical line.
        if ch == "." and self._peek(1) != "." and self.tokens:
            self.at_line_start = False
            return False
        if indent % 4 != 0:
            raise indent_error(
                "indentation must be a multiple of four spaces",
                Span.point(self.source, self.line, 1),
                "align the line to an AXON indentation level",
            )
        level = indent // 4
        current = self.indent_stack[-1]
        if level > current:
            if level != current + 1:
                raise indent_error(
                    "unexpected indentation jump",
                    Span.point(self.source, self.line, 1),
                    "indent exactly one level (four spaces) at a time",
                )
            self.indent_stack.append(level)
            self._emit("INDENT", "", self.line, self.col)
        else:
            while self.indent_stack[-1] > level:
                self.indent_stack.pop()
                self._emit("DEDENT", "", self.line, self.col)
            if self.indent_stack[-1] != level:
                raise indent_error(
                    "dedent does not match any outer indentation level",
                    Span.point(self.source, self.line, 1),
                )
        self.at_line_start = False
        return False

    def _skip_comment(self) -> None:
        if self._peek(1) == "#" and self._peek(2) == "#":
            # Block comment: ### ... ###
            start_line, start_col = self.line, self.col
            self._advance(); self._advance(); self._advance()
            while self.pos < self.n:
                if self._peek() == "#" and self._peek(1) == "#" and self._peek(2) == "#":
                    self._advance(); self._advance(); self._advance()
                    return
                self._advance()
            raise lex_error(
                "unterminated block comment",
                Span.point(self.source, start_line, start_col),
                "close the block comment with ###",
            )
        # Line comment: ## ...
        while self.pos < self.n and self._peek() != "\n":
            self._advance()

    def _scan_token(self) -> None:
        ch = self._peek()
        if ch == " ":
            self._advance()
            return
        if ch == "\t":
            self._advance()
            return
        if ch == "#":
            self._skip_comment()
            return
        if ch == "\\" and self._rest_of_line_blank(1):
            # Explicit line continuation.
            while self.pos < self.n and self._peek() != "\n":
                self._advance()
            if self._peek() == "\n":
                self._advance()
            return
        if ch == "\n":
            self._advance()
            if self.paren_depth > 0:
                return
            if self._next_line_is_chain():
                return
            if self.tokens and self.tokens[-1].kind not in {"NEWLINE", "INDENT", "DEDENT"}:
                self._emit("NEWLINE", "", self.line, self.col)
            self.at_line_start = True
            return
        if ch in OPEN_BRACKETS:
            self.paren_depth += 1
            self._scan_operator()
            return
        if ch in CLOSE_BRACKETS:
            if self.paren_depth > 0:
                self.paren_depth -= 1
            self._scan_operator()
            return
        if ch.isdigit():
            self._scan_number()
            return
        if ch == "f" and self._peek(1) == '"':
            self._scan_string("FSTRING", prefix=1)
            return
        if ch == "r" and self._peek(1) == '"':
            self._scan_string("RAWSTRING", prefix=1)
            return
        if ch == '"':
            self._scan_string("STRING", prefix=0)
            return
        if ch == "'":
            self._scan_char()
            return
        if ch.isalpha() or ch == "_":
            self._scan_name()
            return
        self._scan_operator()

    def _rest_of_line_blank(self, offset: int) -> bool:
        i = self.pos + offset
        while i < self.n and self.text[i] in " \t":
            i += 1
        return i >= self.n or self.text[i] == "\n"

    def _next_line_is_chain(self) -> bool:
        """After a newline, does the next logical line begin with `.method`?"""
        i = self.pos
        while i < self.n:
            # skip indentation spaces
            while i < self.n and self.text[i] == " ":
                i += 1
            if i >= self.n:
                return False
            ch = self.text[i]
            if ch == "\n":
                i += 1
                continue
            if ch == "#":
                while i < self.n and self.text[i] != "\n":
                    i += 1
                continue
            return ch == "." and (i + 1 >= self.n or self.text[i + 1] != ".")
        return False

    def _scan_number(self) -> None:
        start_line, start_col, start = self.line, self.col, self.pos
        kind = "INT"
        if self._peek() == "0" and self._peek(1) in ("x", "X", "b", "B", "o", "O"):
            self._advance(); self._advance()
            while self._peek() and (self._peek().isalnum() or self._peek() == "_"):
                self._advance()
        else:
            while self._peek().isdigit() or (self._peek() == "_" and self._peek(1).isdigit()):
                self._advance()
            if self._peek() == "." and self._peek(1).isdigit():
                kind = "FLOAT"
                self._advance()
                while self._peek().isdigit() or (self._peek() == "_" and self._peek(1).isdigit()):
                    self._advance()
            if self._peek() in ("e", "E") and (self._peek(1).isdigit() or self._peek(1) in ("+", "-")):
                kind = "FLOAT"
                self._advance()
                if self._peek() in ("+", "-"):
                    self._advance()
                while self._peek().isdigit() or (self._peek() == "_" and self._peek(1).isdigit()):
                    self._advance()
            # numeric type suffix: 1_u64, 3.0_f32
            if self._peek() == "_" and self._peek(1).isalpha():
                self._advance()
                while self._peek().isalnum():
                    self._advance()
        self._emit(kind, self.text[start:self.pos], start_line, start_col)

    def _scan_string(self, kind: str, prefix: int) -> None:
        start_line, start_col, start = self.line, self.col, self.pos
        for _ in range(prefix):
            self._advance()
        # triple-quoted heredoc
        if self._peek() == '"' and self._peek(1) == '"' and self._peek(2) == '"':
            self._advance(); self._advance(); self._advance()
            while self.pos < self.n:
                if self._peek() == '"' and self._peek(1) == '"' and self._peek(2) == '"':
                    self._advance(); self._advance(); self._advance()
                    self._emit(kind, self.text[start:self.pos], start_line, start_col)
                    return
                self._advance()
            raise lex_error("unterminated string literal", Span.point(self.source, start_line, start_col))
        self._advance()  # opening quote
        depth = 0  # f-string interpolation nesting
        while self.pos < self.n:
            c = self._peek()
            if kind == "FSTRING":
                if c == "{":
                    if depth == 0 and self._peek(1) == "{":
                        self._advance(); self._advance()  # `{{` escape
                        continue
                    depth += 1
                elif c == "}" and depth > 0:
                    depth -= 1
                elif c == '"' and depth > 0:
                    self._skip_nested_string(start_line, start_col)
                    continue
            if c == "\\" and kind != "RAWSTRING":
                self._advance(); self._advance()
                continue
            if c == "\\" and kind == "RAWSTRING":
                self._advance(); self._advance()
                continue
            if c == '"':
                self._advance()
                self._emit(kind, self.text[start:self.pos], start_line, start_col)
                return
            if c == "\n":
                raise lex_error(
                    "unterminated string literal",
                    Span.point(self.source, start_line, start_col),
                    "strings cannot span lines; use a triple-quoted string",
                )
            self._advance()
        raise lex_error("unterminated string literal", Span.point(self.source, start_line, start_col))

    def _skip_nested_string(self, start_line: int, start_col: int) -> None:
        """Skip a string literal inside an f-string `{...}` interpolation."""
        self._advance()  # opening quote
        while self.pos < self.n and self._peek() != '"':
            if self._peek() == "\n":
                break
            if self._peek() == "\\":
                self._advance()
            self._advance()
        if self._peek() != '"':
            raise lex_error("unterminated string literal",
                            Span.point(self.source, start_line, start_col))
        self._advance()

    def _scan_char(self) -> None:
        start_line, start_col, start = self.line, self.col, self.pos
        self._advance()  # opening quote
        if self._peek() == "\\":
            self._advance(); self._advance()
        elif self._peek() and self._peek() != "'":
            self._advance()
        if self._peek() != "'":
            raise lex_error(
                "invalid character literal",
                Span.point(self.source, start_line, start_col),
                "character literals contain exactly one Unicode scalar value",
            )
        self._advance()
        self._emit("CHAR", self.text[start:self.pos], start_line, start_col)

    def _scan_name(self) -> None:
        start_line, start_col, start = self.line, self.col, self.pos
        while self._peek() and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        value = self.text[start:self.pos]
        kind = "KW" if value in KEYWORDS else "NAME"
        self._emit(kind, value, start_line, start_col)

    def _scan_operator(self) -> None:
        start_line, start_col = self.line, self.col
        for op in OPERATORS:
            if self.text.startswith(op, self.pos):
                for _ in op:
                    self._advance()
                self._emit("OP", op, start_line, start_col)
                return
        bad = self._peek()
        raise lex_error(
            f"unexpected character {bad!r}",
            Span.point(self.source, start_line, start_col),
        )


def tokenize(source: Source) -> list[Token]:
    return Lexer(source).lex()
