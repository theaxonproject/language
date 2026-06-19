"""Diagnostics for the AXON compiler.

Every user-facing error carries a stable error code, a human-readable title, a
message, and an optional source span. Spans point into a `Source` object so the
diagnostic renderer can show the offending line with a caret.
"""

from __future__ import annotations

import dataclasses
import pathlib


@dataclasses.dataclass(frozen=True)
class Source:
    """A single AXON source file held in memory."""

    path: pathlib.Path
    text: str

    @classmethod
    def from_path(cls, path: pathlib.Path) -> "Source":
        return cls(path, path.read_text(encoding="utf-8"))

    @classmethod
    def from_text(cls, name: str, text: str) -> "Source":
        return cls(pathlib.Path(name), text)

    def line_text(self, line: int) -> str:
        lines = self.text.splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1]
        return ""


@dataclasses.dataclass(frozen=True)
class Span:
    """A half-open region of a source file, 1-based line/column."""

    source: Source
    line: int
    column: int
    end_line: int
    end_column: int

    @classmethod
    def point(cls, source: Source, line: int, column: int) -> "Span":
        return cls(source, line, column, line, column)

    def to(self, other: "Span") -> "Span":
        return Span(self.source, self.line, self.column, other.end_line, other.end_column)


# Error code registry. Codes are part of the public toolchain contract and must
# remain stable across releases.
ERROR_CODES = {
    "E001": "invalid source text",
    "E002": "invalid indentation",
    "E003": "unterminated literal",
    "E050": "syntax error",
    "E051": "unexpected token",
    "E052": "expected an indented block",
    "E100": "unknown symbol",
    "E101": "duplicate definition",
    "E102": "unknown type",
    "E103": "unknown field",
    "E104": "unknown method",
    "E110": "type mismatch",
    "E111": "wrong number of arguments",
    "E112": "non-exhaustive match",
    "E113": "cannot infer type",
    "E120": "missing main function",
    "E121": "invalid return",
    "E200": "unresolved import",
    "E800": "code generation error",
    "E810": "C compiler error",
    "E850": "runtime error",
    "E999": "internal compiler error",
}


class Diagnostic(Exception):
    """A user-facing compiler diagnostic."""

    def __init__(
        self,
        code: str,
        message: str,
        span: Span | None = None,
        help_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.title = ERROR_CODES.get(code, "error")
        self.message = message
        self.span = span
        self.help_text = help_text

    def render(self, color: bool = False) -> str:
        def paint(text: str, code: str) -> str:
            return f"\033[{code}m{text}\033[0m" if color else text

        header = paint(f"error[{self.code}]", "1;31") + f": {self.title}"
        out = [header, f"   = {self.message}"]
        if self.span is not None:
            sp = self.span
            out.append(f"  --> {sp.source.path}:{sp.line}:{sp.column}")
            line_text = sp.source.line_text(sp.line)
            gutter = f"{sp.line} | "
            out.append(f"{gutter}{line_text}")
            width = max(1, (sp.end_column - sp.column) if sp.end_line == sp.line else 1)
            caret = " " * (len(gutter) + sp.column - 1) + paint("^" * width, "1;31")
            out.append(caret)
        if self.help_text:
            out.append(f"   = help: {self.help_text}")
        out.append(f"   = see: https://m5rcel.neocities.org/axon/errors/{self.code}.xhtml")
        return "\n".join(out)


# Convenience constructors for the most common diagnostic categories. They exist
# so call sites read clearly and so codes are not duplicated as string literals.
def lex_error(message: str, span: Span | None = None, help_text: str | None = None) -> Diagnostic:
    return Diagnostic("E001", message, span, help_text)


def indent_error(message: str, span: Span | None = None, help_text: str | None = None) -> Diagnostic:
    return Diagnostic("E002", message, span, help_text)


def parse_error(message: str, span: Span | None = None, help_text: str | None = None) -> Diagnostic:
    return Diagnostic("E050", message, span, help_text)
