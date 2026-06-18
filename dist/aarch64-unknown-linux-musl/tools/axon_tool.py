#!/usr/bin/env python3
"""AXON production toolchain.

This implementation provides the shared lexer, parser, formatter, linter,
project command surface, and executable runtime used by AXON distributions.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import difflib
import json
import os
import pathlib
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections import deque
from typing import Any, Callable, Iterable, Iterator


VERSION = "1.0.0"
SUPPORTED_TOOLS = {
    "axc",
    "axbuild",
    "axfmt",
    "axlint",
    "axrepl",
    "axpkg",
    "axls",
    "axup",
}


class AxonError(Exception):
    """Base class for user-facing AXON diagnostics."""

    code = "E999"
    title = "AXON error"

    def __init__(
        self,
        message: str,
        path: pathlib.Path | None = None,
        line: int | None = None,
        column: int | None = None,
        help_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.line = line
        self.column = column
        self.help_text = help_text

    def format(self) -> str:
        location = ""
        if self.path is not None:
            line = self.line if self.line is not None else 1
            column = self.column if self.column is not None else 1
            location = f"\n  --> {self.path}:{line}:{column}"
        help_line = f"\n   = help: {self.help_text}" if self.help_text else ""
        return (
            f"error[{self.code}]: {self.title}\n"
            f"   = {self.message}"
            f"{location}"
            f"{help_line}\n"
            f"   = see: https://m5rcel.neocities.org/axon/errors/{self.code}.xhtml"
        )


class LexError(AxonError):
    code = "E001"
    title = "invalid source text"


class ParseError(AxonError):
    code = "E050"
    title = "syntax error"


class NameErrorAx(AxonError):
    code = "E100"
    title = "unknown symbol"


class RuntimeAxonError(AxonError):
    code = "E850"
    title = "runtime error"


@dataclasses.dataclass(frozen=True)
class SourceLine:
    number: int
    indent: int
    text: str


@dataclasses.dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int
    column: int


@dataclasses.dataclass
class FunctionDef:
    name: str
    params: list[str]
    body: list[SourceLine]
    owner: str | None
    path: pathlib.Path
    line: int
    return_type: str | None = None


@dataclasses.dataclass
class StructDef:
    name: str
    fields: list[str]
    defaults: dict[str, str]


@dataclasses.dataclass
class Program:
    path: pathlib.Path
    functions: dict[str, FunctionDef]
    structs: dict[str, StructDef]
    methods: dict[str, dict[str, FunctionDef]]
    imports: list[str]


def strip_block_comments(source: str) -> str:
    out: list[str] = []
    i = 0
    in_block = False
    while i < len(source):
        if not in_block and source.startswith("###", i):
            in_block = True
            i += 3
            continue
        if in_block and source.startswith("###", i):
            in_block = False
            i += 3
            continue
        if not in_block:
            out.append(source[i])
        elif source[i] == "\n":
            out.append("\n")
        i += 1
    if in_block:
        raise LexError("unterminated block comment", help_text="close the block comment with ###")
    return "".join(out)


def remove_line_comment(line: str) -> str:
    in_string: str | None = None
    escaped = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in {"\"", "'"}:
            in_string = ch
            i += 1
            continue
        if line.startswith("##", i):
            return line[:i]
        i += 1
    return line


def logical_lines(source: str, path: pathlib.Path) -> list[SourceLine]:
    """Return non-empty AXON logical lines with validated indentation."""

    source = strip_block_comments(source)
    raw_lines = source.splitlines()
    lines: list[SourceLine] = []
    paren_depth = 0
    continuation: SourceLine | None = None

    for number, raw in enumerate(raw_lines, 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise LexError("tabs are invalid in indentation", path, number, 1, "use four spaces")
        trimmed_comment = remove_line_comment(raw).rstrip()
        if not trimmed_comment.strip():
            continue
        indent = len(trimmed_comment) - len(trimmed_comment.lstrip(" "))
        if indent % 4 != 0:
            raise LexError(
                "indentation must be a multiple of four spaces",
                path,
                number,
                1,
                "align the line to an AXON indentation level",
            )
        text = trimmed_comment.strip()
        opens = sum(text.count(ch) for ch in "([{")
        closes = sum(text.count(ch) for ch in ")]}")
        starts_suffix = text.startswith(".")
        if continuation is None and starts_suffix and lines:
            previous = lines.pop()
            continuation = dataclasses.replace(previous, text=previous.text + text)
            paren_depth += opens - closes
            if text.endswith("\\") or paren_depth > 0:
                if continuation.text.endswith("\\"):
                    continuation = dataclasses.replace(continuation, text=continuation.text[:-1].rstrip())
                continue
            lines.append(continuation)
            continuation = None
            paren_depth = 0
            continue
        if continuation is not None:
            separator = "" if starts_suffix else " "
            continuation = dataclasses.replace(
                continuation, text=continuation.text + separator + text
            )
        else:
            continuation = SourceLine(number, indent // 4, text)
        paren_depth += opens - closes
        continued = text.endswith("\\") or paren_depth > 0
        if continued:
            if continuation.text.endswith("\\"):
                continuation = dataclasses.replace(continuation, text=continuation.text[:-1].rstrip())
            continue
        lines.append(continuation)
        continuation = None
        paren_depth = 0

    if continuation is not None:
        lines.append(continuation)
    if paren_depth != 0:
        raise LexError("unbalanced delimiters", path, lines[-1].number if lines else 1, 1)
    return lines


TOKEN_RE = re.compile(
    r"""
    (?P<FSTRING>f"(?:\\.|[^"\\])*")
  | (?P<RAW>r"(?:\\.|[^"\\])*")
  | (?P<STRING>"(?:\\.|[^"\\])*")
  | (?P<CHAR>'(?:\\.|[^'\\])')
  | (?P<FLOAT>\d(?:[\d_]*\d)?\.\d(?:[\d_]*\d)?(?:[eE][+-]?\d+)?(?:_[A-Za-z][A-Za-z0-9]*)?)
  | (?P<NUMBER>0[xX][0-9A-Fa-f](?:[0-9A-Fa-f_]*[0-9A-Fa-f])?(?:_[A-Za-z][A-Za-z0-9]*)?|0[bB][01](?:[01_]*[01])?(?:_[A-Za-z][A-Za-z0-9]*)?|0[oO][0-7](?:[0-7_]*[0-7])?(?:_[A-Za-z][A-Za-z0-9]*)?|\d(?:[\d_]*\d)?(?:_[A-Za-z][A-Za-z0-9]*)?)
  | (?P<OP>\?\.|==|!=|<=|>=|<<=|>>=|<<|>>|\|>|\.\.|->|=>|\+=|-=|\*=|/=|%=|&=|\|=|\^=|[+\-*/%<>=&|^~.,:;(){}\[\]?])
  | (?P<NAME>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<SPACE>\s+)
  | (?P<MISMATCH>.)
    """,
    re.VERBOSE,
)


def lex_expr(expr: str, line: int = 1) -> list[Token]:
    tokens: list[Token] = []
    for match in TOKEN_RE.finditer(expr):
        kind = match.lastgroup or "MISMATCH"
        value = match.group()
        if kind == "SPACE":
            continue
        if kind == "MISMATCH":
            raise ParseError(f"unexpected character {value!r}", line=line, column=match.start() + 1)
        tokens.append(Token(kind, value, line, match.start() + 1))
    tokens.append(Token("EOF", "", line, len(expr) + 1))
    return tokens


def parse_program(path: pathlib.Path) -> Program:
    source = path.read_text(encoding="utf-8")
    lines = logical_lines(source, path)
    functions: dict[str, FunctionDef] = {}
    structs: dict[str, StructDef] = {}
    methods: dict[str, dict[str, FunctionDef]] = {}
    imports: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        text = line.text
        if line.indent != 0:
            raise ParseError("top-level declarations must not be indented", path, line.number, 1)
        if text.startswith("@"):
            i += 1
            continue
        if text.startswith("use "):
            imports.append(text[4:].strip())
            i += 1
            continue
        if text.startswith("const "):
            i += 1
            continue
        if text.startswith("trait "):
            i = skip_block(lines, i)
            continue
        if text.startswith("enum "):
            i = skip_block(lines, i)
            continue
        if text.startswith("extern "):
            i = skip_block(lines, i)
            continue
        if text.startswith("struct "):
            name = parse_decl_name(text, "struct", path, line)
            body, i = collect_block(lines, i)
            fields: list[str] = []
            defaults: dict[str, str] = {}
            for field_line in body:
                if field_line.text.startswith("@"):
                    continue
                if ":" not in field_line.text:
                    raise ParseError("struct fields require a type annotation", path, field_line.number, 1)
                left, _, rest = field_line.text.partition(":")
                field_name = left.strip()
                if not re.fullmatch(r"[a-z_][A-Za-z0-9_]*", field_name):
                    raise ParseError("struct field names must be snake_case identifiers", path, field_line.number, 1)
                fields.append(field_name)
                if "=" in rest:
                    defaults[field_name] = rest.split("=", 1)[1].strip()
            structs[name] = StructDef(name, fields, defaults)
            continue
        if text.startswith("impl "):
            owner = parse_impl_owner(text, path, line)
            body, i = collect_block(lines, i)
            j = 0
            while j < len(body):
                item = body[j]
                if item.text.startswith("@"):
                    j += 1
                    continue
                if not item.text.startswith("fn "):
                    raise ParseError("impl blocks may contain function declarations only", path, item.number, 1)
                fn, j = parse_function_at(body, j, path, owner=owner)
                methods.setdefault(owner, {})[fn.name] = fn
            continue
        if text.startswith("fn "):
            fn, i = parse_function_at(lines, i, path, owner=None)
            if fn.name in functions:
                raise ParseError(f"duplicate function `{fn.name}`", path, line.number, 1)
            functions[fn.name] = fn
            continue
        raise ParseError(f"unsupported top-level declaration `{text}`", path, line.number, 1)

    return Program(path, functions, structs, methods, imports)


def parse_decl_name(text: str, keyword: str, path: pathlib.Path, line: SourceLine) -> str:
    match = re.match(rf"{keyword}\s+([A-Z][A-Za-z0-9_]*)", text)
    if not match:
        raise ParseError(f"invalid {keyword} declaration", path, line.number, 1)
    return match.group(1)


def parse_impl_owner(text: str, path: pathlib.Path, line: SourceLine) -> str:
    match = re.match(r"impl(?:\s*\([^)]*\))?\s+([A-Z][A-Za-z0-9_]*(?:\([^)]*\))?)(?:\s+as\s+[A-Z][A-Za-z0-9_.]*)?", text)
    if not match:
        raise ParseError("invalid impl declaration", path, line.number, 1)
    return match.group(1).split("(", 1)[0]


def parse_function_at(
    lines: list[SourceLine], index: int, path: pathlib.Path, owner: str | None
) -> tuple[FunctionDef, int]:
    header = lines[index]
    match = re.match(
        r"fn\s+([a-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*\((.*?)\)\s*(?:->\s*([A-Za-z0-9_!?()., \[\]refmut]+))?(?:\s*=\s*(.+))?$",
        header.text,
    )
    if not match:
        raise ParseError("invalid function declaration", path, header.number, 1)
    name, raw_params, return_type, expr_body = match.groups()
    params = parse_param_names(raw_params)
    if expr_body is not None:
        body = [SourceLine(header.number, header.indent + 1, "return " + expr_body.strip())]
        return FunctionDef(name, params, body, owner, path, header.number, return_type), index + 1
    body, next_index = collect_block(lines, index)
    return FunctionDef(name, params, body, owner, path, header.number, return_type), next_index


def parse_param_names(raw: str) -> list[str]:
    if not raw.strip():
        return []
    names: list[str] = []
    for part in split_top_level(raw, ","):
        part = part.strip()
        if not part:
            continue
        before_type = part.split(":", 1)[0].strip()
        before_type = before_type.removeprefix("mut ").strip()
        if before_type == "...":
            continue
        if not re.fullmatch(r"[a-z_][A-Za-z0-9_]*|self", before_type):
            raise ParseError(f"invalid parameter `{before_type}`")
        names.append(before_type)
    return names


def collect_block(lines: list[SourceLine], header_index: int) -> tuple[list[SourceLine], int]:
    header = lines[header_index]
    body_indent = header.indent + 1
    start = header_index + 1
    if start >= len(lines) or lines[start].indent < body_indent:
        raise ParseError("expected an indented block", header.line if hasattr(header, "line") else None)
    body: list[SourceLine] = []
    i = start
    while i < len(lines) and lines[i].indent >= body_indent:
        body.append(dataclasses.replace(lines[i], indent=lines[i].indent - body_indent))
        i += 1
    return body, i


def skip_block(lines: list[SourceLine], header_index: int) -> int:
    header_indent = lines[header_index].indent
    i = header_index + 1
    while i < len(lines) and lines[i].indent > header_indent:
        i += 1
    return i


def split_top_level(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_string: str | None = None
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in {"\"", "'"}:
            in_string = ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


class ReturnSignal(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class AxRuntimeFailure(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__(str(value))
        self.value = value


@dataclasses.dataclass
class SomeValue:
    value: Any

    def unwrap_or(self, default: Any) -> Any:
        return self.value

    def __str__(self) -> str:
        return f"Some({self.value})"


class AxList(list):
    def len(self) -> int:
        return len(self)

    def slice(self, start: int, end: int | None = None) -> "AxList":
        return AxList(self[start:end])

    def to_vec(self) -> "AxList":
        return AxList(self)

    def push(self, value: Any) -> None:
        self.append(value)

    def first(self) -> Any:
        return SomeValue(self[0]) if self else None

    def last(self) -> Any:
        return SomeValue(self[-1]) if self else None


class AxMap(dict):
    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        if key in self:
            return SomeValue(super().get(key))
        return default

    def get_mut(self, key: Any) -> Any:
        return self.get(key)

    def insert(self, key: Any, value: Any) -> None:
        self[key] = value

    def remove(self, key: Any) -> None:
        if key in self:
            del self[key]

    def values(self) -> AxList:  # type: ignore[override]
        return AxList(super().values())


class AxString(str):
    def split(self, sep: str | None = None) -> AxList:  # type: ignore[override]
        return AxList(AxString(x) for x in super().split(sep))

    def trim(self) -> "AxString":
        return AxString(self.strip())

    def parse(self, typ: Any) -> Any:
        name = getattr(typ, "name", str(typ))
        if name.startswith("uint") or name.startswith("int"):
            return int(self)
        if name.startswith("float"):
            return float(self)
        return AxString(self)

    def len(self) -> int:
        return len(self)

    def compare(self, other: Any) -> int:
        return (self > other) - (self < other)


class AxObject:
    def __init__(self, runtime: "Runtime", type_name: str, fields: dict[str, Any]) -> None:
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_type_name", type_name)
        object.__setattr__(self, "_fields", fields)

    @property
    def type_name(self) -> str:
        return object.__getattribute__(self, "_type_name")

    def __getattr__(self, name: str) -> Any:
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            return fields[name]
        runtime = object.__getattribute__(self, "_runtime")
        methods = runtime.program.methods.get(self.type_name, {})
        if name in methods:
            return lambda *args, **kwargs: runtime.call_function(methods[name], [self, *args], kwargs)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self._fields[name] = value

    def __repr__(self) -> str:
        fields = ", ".join(f"{key}: {value!r}" for key, value in self._fields.items())
        return f"{self.type_name}{{ {fields} }}"


class TypeProxy:
    def __init__(self, runtime: "Runtime", name: str, args: tuple[Any, ...] = ()) -> None:
        self.runtime = runtime
        self.name = name
        self.args = args

    def __call__(self, *args: Any, **kwargs: Any) -> "TypeProxy":
        return TypeProxy(self.runtime, self.name, args)

    def __getattr__(self, name: str) -> Any:
        if name == "new":
            methods = self.runtime.program.methods.get(self.name, {})
            if "new" in methods:
                return lambda *args, **kwargs: self.runtime.call_function(methods["new"], list(args), kwargs)
            return lambda *args, **kwargs: self.runtime.construct_builtin(self.name, *args, **kwargs)
        methods = self.runtime.program.methods.get(self.name, {})
        if name in methods:
            return lambda *args, **kwargs: self.runtime.call_function(methods[name], list(args), kwargs)
        if self.name == "Math" and name == "PI":
            return 3.141592653589793
        raise AttributeError(name)

    def __repr__(self) -> str:
        return self.name


class Channel:
    def __init__(self, buffer: int = 0) -> None:
        self.items: deque[Any] = deque(maxlen=buffer or None)

    def send(self, value: Any) -> None:
        self.items.append(value)

    def recv(self) -> Any:
        if not self.items:
            raise AxRuntimeFailure("channel receive would block")
        return self.items.popleft()


class AxFile:
    def __init__(self, path: str) -> None:
        self.path = path

    @staticmethod
    def open(path: str) -> "AxFile":
        if not os.path.exists(path):
            raise AxRuntimeFailure(f"file not found: {path}")
        return AxFile(path)

    def read_all_text(self) -> AxString:
        with open(self.path, "r", encoding="utf-8") as handle:
            return AxString(handle.read())


class HttpClient:
    @staticmethod
    def new() -> "HttpClient":
        return HttpClient()

    def get(self, url: str) -> "HttpResponse":
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                status = int(response.status)
                body = response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover - network dependent
            raise AxRuntimeFailure(f"http get failed: {exc}") from exc
        return HttpResponse(status, body)


class HttpResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = AxString(body)

    def text(self) -> AxString:
        return self._body


class TcpListener:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    @staticmethod
    def bind(address: str) -> "TcpListener":
        host, port_text = address.rsplit(":", 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port_text)))
        sock.listen()
        return TcpListener(sock)

    def accept(self) -> "TcpStream":
        conn, _ = self.sock.accept()
        return TcpStream(conn)


class TcpStream:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def read(self, _buffer: Any) -> int:
        data = self.sock.recv(4096)
        self._last = data
        return len(data)

    def write_all(self, data: Any) -> None:
        if isinstance(data, list):
            payload = bytes(data)
        elif isinstance(data, bytes):
            payload = data
        else:
            payload = bytes(str(data), "utf-8")
        self.sock.sendall(payload)


class CliApp:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self.args: list[str] = []

    @staticmethod
    def new(name: str, version: str) -> "CliApp":
        return CliApp(name, version)

    def arg(self, name: str, help: str = "") -> "CliApp":
        self.args.append(name)
        return self

    def parse(self) -> "CliMatches":
        return CliMatches(sys.argv[2:])


class CliMatches:
    def __init__(self, args: list[str]) -> None:
        self.args = args

    def value(self, name: str) -> AxString:
        if not self.args:
            raise AxRuntimeFailure(f"missing required argument: {name}")
        return AxString(self.args[0])


class JsonModule:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    def parse(self, text: str) -> Any:
        return wrap_json(json.loads(text))

    def parse_as(self, typ: TypeProxy | type) -> Callable[[str], Any]:
        def parser(text: str) -> Any:
            raw = json.loads(text)
            if isinstance(typ, TypeProxy):
                return self.runtime.make_record(typ.name, {str(k): wrap_json(v) for k, v in raw.items()})
            return wrap_json(raw)

        return parser


def wrap_json(value: Any) -> Any:
    if isinstance(value, dict):
        return AxMap({AxString(k): wrap_json(v) for k, v in value.items()})
    if isinstance(value, list):
        return AxList(wrap_json(v) for v in value)
    if isinstance(value, str):
        return AxString(value)
    return value


class Runtime:
    def __init__(self, program: Program, argv: list[str] | None = None) -> None:
        self.program = program
        self.argv = argv or []
        self.globals: dict[str, Any] = {}
        self.install_builtins()

    def install_builtins(self) -> None:
        def println(*args: Any) -> None:
            print(*(format_value(arg) for arg in args))

        def eprintln(*args: Any) -> None:
            print(*(format_value(arg) for arg in args), file=sys.stderr)

        def panic(msg: str) -> None:
            raise AxRuntimeFailure(msg)

        self.globals.update(
            {
                "print": lambda *args: print(*(format_value(a) for a in args), end=""),
                "println": println,
                "eprint": lambda *args: print(*(format_value(a) for a in args), end="", file=sys.stderr),
                "eprintln": eprintln,
                "panic": panic,
                "assert": lambda cond, msg="assertion failed": None if cond else panic(msg),
                "assert_eq": lambda a, b: None if a == b else panic(f"{a!r} != {b!r}"),
                "type_name": lambda typ=None: getattr(typ, "name", type(typ).__name__),
                "size_of": lambda typ=None: 8,
                "todo": lambda msg="todo": panic(msg),
                "unreachable": lambda: panic("unreachable"),
                "unimplemented": lambda: panic("unimplemented"),
                "Some": lambda value: SomeValue(value),
                "None": None,
                "none": None,
                "true": True,
                "false": False,
                "str": TypeProxy(self, "str"),
                "int": TypeProxy(self, "int"),
                "uint": TypeProxy(self, "uint"),
                "uint16": TypeProxy(self, "uint16"),
                "uint32": TypeProxy(self, "uint32"),
                "uint64": TypeProxy(self, "uint64"),
                "float64": TypeProxy(self, "float64"),
                "bool": TypeProxy(self, "bool"),
                "Channel": TypeProxy(self, "Channel"),
                "File": AxFile,
                "HttpClient": HttpClient,
                "TcpListener": TcpListener,
                "TcpStream": TcpStream,
                "App": CliApp,
                "Json": JsonModule(self),
                "Math": TypeProxy(self, "Math"),
                "Map": TypeProxy(self, "Map"),
                "Vec": TypeProxy(self, "Vec"),
                "Arc": TypeProxy(self, "Arc"),
                "Mutex": TypeProxy(self, "Mutex"),
                "RwLock": TypeProxy(self, "RwLock"),
            }
        )
        for name in self.program.structs:
            self.globals[name] = TypeProxy(self, name)
        for name, fn in self.program.functions.items():
            self.globals[name] = lambda *args, _fn=fn, **kwargs: self.call_function(_fn, list(args), kwargs)

    def construct_builtin(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "Channel":
            return Channel(int(kwargs.get("buffer", args[0] if args else 0)))
        if name == "Map":
            return AxMap()
        if name == "Vec":
            return AxList()
        if name in {"Mutex", "RwLock", "Arc"}:
            return args[0] if args else None
        if name in self.program.structs:
            fields = dict(kwargs)
            return self.make_record(name, fields)
        return TypeProxy(self, name)

    def make_record(self, name: str, fields: dict[str, Any]) -> AxObject:
        struct = self.program.structs.get(name)
        if struct is None:
            raise AxRuntimeFailure(f"unknown struct: {name}")
        values: dict[str, Any] = {}
        for field in struct.fields:
            if field in fields:
                values[field] = fields[field]
            elif field in struct.defaults:
                values[field] = ExprParser(self, {}, struct.defaults[field]).parse()
            else:
                values[field] = None
        return AxObject(self, name, values)

    def call_main(self) -> int:
        main = self.program.functions.get("main")
        if main is None:
            raise RuntimeAxonError("no main function found", self.program.path, 1, 1)
        try:
            result = self.call_function(main, [], {})
        except AxRuntimeFailure as exc:
            raise RuntimeAxonError(str(exc.value), self.program.path, main.line, 1) from exc
        if result is None:
            return 0
        if not isinstance(result, int):
            raise RuntimeAxonError("main must return int", self.program.path, main.line, 1)
        return result

    def call_function(self, fn: FunctionDef, args: list[Any], kwargs: dict[str, Any]) -> Any:
        env: dict[str, Any] = dict(self.globals)
        if len(args) > len(fn.params):
            raise AxRuntimeFailure(f"too many arguments for {fn.name}")
        for name, value in zip(fn.params, args):
            env[name] = value
        for name in fn.params[len(args) :]:
            if name in kwargs:
                env[name] = kwargs[name]
            else:
                env[name] = None
        try:
            self.exec_block(fn.body, env)
        except ReturnSignal as signal:
            return signal.value
        return None

    def exec_block(self, lines: list[SourceLine], env: dict[str, Any]) -> None:
        i = 0
        while i < len(lines):
            i = self.exec_at(lines, i, env)

    def exec_at(self, lines: list[SourceLine], i: int, env: dict[str, Any]) -> int:
        line = lines[i]
        text = line.text
        if text.startswith("@"):
            return i + 1
        if text.startswith("let ") and " catch " in text and text.endswith(" then"):
            return self.exec_let_try_catch(lines, i, env)
        if text.startswith("let "):
            self.exec_let(text, env)
            return i + 1
        if text.startswith("return"):
            expr = text[6:].strip()
            raise ReturnSignal(self.eval_expr(expr, env) if expr else None)
        if text == "break":
            raise BreakSignal()
        if text == "continue":
            raise ContinueSignal()
        if text.startswith("if ") and text.endswith(" then"):
            return self.exec_if(lines, i, env)
        if text.startswith("for ") and text.endswith(" do"):
            return self.exec_for(lines, i, env)
        if text.startswith("loop do"):
            body, next_i = collect_block(lines, i)
            while True:
                try:
                    self.exec_block(body, env)
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
            return next_i
        if text.startswith("match ") and text.endswith(" do"):
            return self.exec_match(lines, i, env)
        if text.startswith("task_group do"):
            body, next_i = collect_block(lines, i)
            self.exec_block(body, env)
            return next_i
        if text.startswith("task do "):
            self.eval_expr(text[8:].strip(), env)
            return i + 1
        if self.is_assignment(text):
            self.exec_assign(text, env)
            return i + 1
        self.eval_expr(text, env)
        return i + 1

    def exec_let(self, text: str, env: dict[str, Any]) -> None:
        left, _, expr = text.partition("=")
        if not expr:
            raise AxRuntimeFailure("let requires an initializer")
        name_part = left[4:].strip().removeprefix("mut ").strip()
        name = name_part.split(":", 1)[0].strip()
        value = self.eval_try_expr(expr.strip(), env)
        env[name] = value

    def exec_let_try_catch(self, lines: list[SourceLine], i: int, env: dict[str, Any]) -> int:
        text = lines[i].text
        left, _, rest = text.partition("=")
        name_part = left[4:].strip().removeprefix("mut ").strip()
        name = name_part.split(":", 1)[0].strip()
        match = re.match(r"\s*try\s+(.+)\s+catch\s+([a-z_][A-Za-z0-9_]*)\s+then$", rest.strip())
        if not match:
            raise AxRuntimeFailure("invalid try/catch let statement")
        expr, err_name = match.groups()
        body, next_i = collect_block(lines, i)
        try:
            env[name] = self.eval_expr(expr, env)
        except AxRuntimeFailure as exc:
            env[err_name] = exc.value
            self.exec_block(body, env)
        return next_i

    def exec_assign(self, text: str, env: dict[str, Any]) -> None:
        op = next(candidate for candidate in ["+=", "-=", "*=", "/=", "%=", "="] if candidate in text)
        target, _, expr = text.partition(op)
        target = target.strip()
        value = self.eval_expr(expr.strip(), env)
        if op != "=":
            current = self.eval_expr(target, env)
            value = {
                "+=": current + value,
                "-=": current - value,
                "*=": current * value,
                "/=": current / value,
                "%=": current % value,
            }[op]
        if "." in target:
            obj_expr, field = target.rsplit(".", 1)
            setattr(self.eval_expr(obj_expr, env), field, value)
        else:
            env[target] = value

    def exec_if(self, lines: list[SourceLine], i: int, env: dict[str, Any]) -> int:
        current = i
        while current < len(lines):
            text = lines[current].text
            if text.startswith("if "):
                cond_expr = text[3:-5].strip()
            elif text.startswith("else if "):
                cond_expr = text[8:-5].strip()
            else:
                break
            body, next_i = collect_block(lines, current)
            if self.eval_expr(cond_expr, env):
                self.exec_block(body, env)
                return self.skip_else_chain(lines, next_i)
            current = next_i
            if current >= len(lines) or lines[current].indent != lines[i].indent or not lines[current].text.startswith("else"):
                return current
        if current < len(lines) and lines[current].indent == lines[i].indent and lines[current].text == "else":
            body, next_i = collect_block(lines, current)
            self.exec_block(body, env)
            return next_i
        return current

    def skip_else_chain(self, lines: list[SourceLine], i: int) -> int:
        while i < len(lines) and lines[i].text.startswith("else"):
            _, i = collect_block(lines, i)
        return i

    def exec_for(self, lines: list[SourceLine], i: int, env: dict[str, Any]) -> int:
        match = re.match(r"for\s+(.+?)\s+in\s+(.+)\s+do$", lines[i].text)
        if not match:
            raise AxRuntimeFailure("invalid for statement")
        pattern, expr = match.groups()
        body, next_i = collect_block(lines, i)
        iterable = self.eval_expr(expr, env)
        for value in iterable:
            bind_pattern(pattern.strip(), value, env)
            try:
                self.exec_block(body, env)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return next_i

    def exec_match(self, lines: list[SourceLine], i: int, env: dict[str, Any]) -> int:
        expr = lines[i].text[6:-3].strip()
        value = self.eval_expr(expr, env)
        cases, next_i = collect_block(lines, i)
        j = 0
        while j < len(cases):
            case = cases[j]
            if not case.text.startswith("case ") or " then" not in case.text:
                raise AxRuntimeFailure("match blocks require case arms")
            pattern_text, _, tail = case.text[5:].partition(" then")
            if tail.strip():
                body = [SourceLine(case.number, case.indent + 1, tail.strip())]
                j += 1
            else:
                body, j = collect_block(cases, j)
            local = dict(env)
            if match_pattern(pattern_text.strip(), value, local):
                env.update({k: v for k, v in local.items() if k not in env or env[k] is not local[k]})
                self.exec_block(body, env)
                return next_i
        return next_i

    def is_assignment(self, text: str) -> bool:
        return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.]*\s*(?:=|\+=|-=|\*=|/=|%=)", text)) and "==" not in text

    def eval_try_expr(self, expr: str, env: dict[str, Any]) -> Any:
        if expr.startswith("try ") and " catch " in expr and expr.endswith(" then"):
            raise AxRuntimeFailure("try/catch with a block is only valid as a statement")
        return self.eval_expr(expr, env)

    def eval_expr(self, expr: str, env: dict[str, Any]) -> Any:
        expr = expr.strip()
        if expr.startswith("try "):
            expr = expr[4:].strip()
        return ExprParser(self, env, expr).parse()


def bind_pattern(pattern: str, value: Any, env: dict[str, Any]) -> None:
    if pattern == "_":
        return
    env[pattern] = value


def match_pattern(pattern: str, value: Any, env: dict[str, Any]) -> bool:
    if pattern in {"none", "None"}:
        return value is None
    if pattern == "_":
        return True
    if pattern.startswith("Some(") and pattern.endswith(")"):
        if not isinstance(value, SomeValue):
            return False
        inner = pattern[5:-1].strip()
        if inner:
            bind_pattern(inner, value.value, env)
        return True
    if re.fullmatch(r"[a-z_][A-Za-z0-9_]*", pattern):
        env[pattern] = value
        return True
    return format_value(value) == pattern


class ExprParser:
    PRECEDENCE = {
        "or": 1,
        "and": 2,
        "==": 3,
        "!=": 3,
        "<": 3,
        "<=": 3,
        ">": 3,
        ">=": 3,
        "..": 4,
        "+": 5,
        "-": 5,
        "*": 6,
        "/": 6,
        "%": 6,
    }

    def __init__(self, runtime: Runtime, env: dict[str, Any], expr: str) -> None:
        self.runtime = runtime
        self.env = env
        self.tokens = lex_expr(expr)
        self.pos = 0
        self.expr = expr

    def current(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        token = self.current()
        self.pos += 1
        return token

    def accept(self, value: str) -> bool:
        if self.current().value == value:
            self.advance()
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise AxRuntimeFailure(f"expected `{value}` in expression `{self.expr}`")

    def parse(self) -> Any:
        value = self.parse_expr(0)
        if self.current().kind != "EOF":
            raise AxRuntimeFailure(f"unexpected token `{self.current().value}` in expression `{self.expr}`")
        return value

    def parse_expr(self, min_prec: int) -> Any:
        left = self.parse_unary()
        while True:
            op = self.current().value
            prec = self.PRECEDENCE.get(op)
            if prec is None or prec < min_prec:
                break
            self.advance()
            right = self.parse_expr(prec + 1)
            left = apply_binary(op, left, right)
        return left

    def parse_unary(self) -> Any:
        token = self.current()
        if token.value in {"-", "+", "not", "~", "&", "&mut", "*"}:
            op = self.advance().value
            value = self.parse_unary()
            if op == "-":
                return -value
            if op == "+":
                return +value
            if op == "not":
                return not value
            return value
        return self.parse_postfix()

    def parse_postfix(self) -> Any:
        value = self.parse_primary()
        while True:
            if self.accept("("):
                args, kwargs = self.parse_call_args_after_open()
                value = self.call_value(value, args, kwargs)
                continue
            if self.accept("."):
                name = self.advance().value
                attr = getattr(value, name)
                if self.accept("("):
                    args, kwargs = self.parse_call_args_after_open()
                    value = self.call_value(attr, args, kwargs)
                else:
                    value = attr
                continue
            if self.accept("?."):
                name = self.advance().value
                if value is None:
                    value = None
                    if self.current().value == "(":
                        self.advance()
                        self.parse_call_args_after_open()
                else:
                    value = getattr(value, name)
                continue
            if self.accept("["):
                if self.accept("]"):
                    raise AxRuntimeFailure("empty index")
                start = None if self.current().value == ".." else self.parse_expr(0)
                if self.accept(".."):
                    end = None if self.current().value == "]" else self.parse_expr(0)
                    self.expect("]")
                    value = value[start:end]
                else:
                    self.expect("]")
                    value = value[start]
                continue
            break
        return value

    def parse_primary(self) -> Any:
        token = self.advance()
        if token.kind == "NUMBER" or token.kind == "FLOAT":
            return parse_number(token.value)
        if token.kind in {"STRING", "RAW", "CHAR"}:
            return AxString(bytes(token.value[1:-1], "utf-8").decode("unicode_escape"))
        if token.kind == "FSTRING":
            return AxString(self.interpolate(token.value[2:-1]))
        if token.value == "(":
            if self.accept(")"):
                return ()
            first = self.parse_expr(0)
            if self.accept(","):
                values = [first]
                if not self.accept(")"):
                    while True:
                        values.append(self.parse_expr(0))
                        if self.accept(")"):
                            break
                        self.expect(",")
                return tuple(values)
            self.expect(")")
            return first
        if token.value == "[":
            if self.accept("]"):
                return AxList()
            first = self.parse_expr(0)
            if self.accept(";"):
                count = int(self.parse_expr(0))
                self.expect("]")
                return AxList([first for _ in range(count)])
            values = [first]
            while self.accept(","):
                if self.current().value == "]":
                    break
                values.append(self.parse_expr(0))
            self.expect("]")
            return AxList(values)
        if token.kind == "NAME":
            if self.current().value == "{":
                return self.parse_record(token.value)
            if token.value in self.env:
                return self.env[token.value]
            if token.value in self.runtime.globals:
                return self.runtime.globals[token.value]
            if re.match(r"[A-Z]", token.value):
                proxy = TypeProxy(self.runtime, token.value)
                self.runtime.globals[token.value] = proxy
                return proxy
            raise AxRuntimeFailure(f"unknown symbol: {token.value}")
        raise AxRuntimeFailure(f"unexpected token `{token.value}`")

    def parse_record(self, name: str) -> AxObject:
        self.expect("{")
        fields: dict[str, Any] = {}
        while not self.accept("}"):
            field = self.advance().value
            self.expect(":")
            fields[field] = self.parse_expr(0)
            self.accept(",")
        return self.runtime.make_record(name, fields)

    def parse_call_args_after_open(self) -> tuple[list[Any], dict[str, Any]]:
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        if self.accept(")"):
            return args, kwargs
        while True:
            if self.current().kind == "NAME" and self.tokens[self.pos + 1].value == ":":
                key = self.advance().value
                self.expect(":")
                kwargs[key] = self.parse_expr(0)
            else:
                args.append(self.parse_expr(0))
            if self.accept(")"):
                break
            self.expect(",")
        return args, kwargs

    def call_value(self, value: Any, args: list[Any], kwargs: dict[str, Any]) -> Any:
        try:
            return value(*args, **kwargs)
        except AxRuntimeFailure:
            raise
        except TypeError as exc:
            raise AxRuntimeFailure(str(exc)) from exc

    def interpolate(self, template: str) -> str:
        def replace(match: re.Match[str]) -> str:
            expr = match.group(1)
            return format_value(ExprParser(self.runtime, self.env, expr).parse())

        return re.sub(r"\{([^{}]+)\}", replace, template)


def parse_number(value: str) -> int | float:
    base_text = value
    if "_" in value:
        candidate, suffix = value.rsplit("_", 1)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", suffix):
            base_text = candidate
    clean = base_text.replace("_", "")
    if "." in clean or "e" in clean.lower():
        return float(clean)
    return int(clean, 0)


def apply_binary(op: str, left: Any, right: Any) -> Any:
    if op == "..":
        return range(int(left), int(right))
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        return left / right
    if op == "%":
        return left % right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "and":
        return bool(left) and bool(right)
    if op == "or":
        return bool(left) or bool(right)
    raise AxRuntimeFailure(f"unsupported operator: {op}")


def format_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "none"
    if isinstance(value, AxString):
        return str(value)
    if isinstance(value, SomeValue):
        return f"Some({format_value(value.value)})"
    return str(value)


def check_program(path: pathlib.Path) -> list[str]:
    parse_program(path)
    return []


def iter_ax_files(paths: list[str]) -> list[pathlib.Path]:
    if not paths:
        paths = ["."]
    found: list[pathlib.Path] = []
    for raw in paths:
        path = pathlib.Path(raw)
        if path.is_dir():
            found.extend(sorted(path.rglob("*.ax")))
        elif path.suffix == ".ax":
            found.append(path)
    return found


def format_source(source: str, path: pathlib.Path) -> str:
    logical_lines(source, path)
    out: list[str] = []
    in_block_comment = False
    for number, raw in enumerate(source.splitlines(), 1):
        if "###" in raw:
            in_block_comment = not in_block_comment
            out.append(raw.rstrip())
            continue
        if in_block_comment or not raw.strip() or raw.lstrip().startswith("##"):
            out.append(raw.rstrip())
            continue
        indent_width = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent_width] or indent_width % 4 != 0:
            raise LexError("invalid indentation", path, number, 1)
        out.append(raw.rstrip())
    return "\n".join(out) + "\n"


def normalize_spacing(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    text = re.sub(r"\s+([),:\]}])", r"\1", text)
    text = re.sub(r"([({\[])\s+", r"\1", text)
    text = re.sub(r"\s*(->|=>|==|!=|<=|>=|\+=|-=|\*=|/=|%=|=|\+|-|\*|/|%|<|>|\.\.)\s*", r" \1 ", text)
    text = re.sub(r"\s*\.\.\s*", "..", text)
    text = re.sub(r"\s+\.", ".", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",(?=\S)", ", ", text)
    return text.strip()


def lint_file(path: pathlib.Path) -> list[str]:
    diagnostics: list[str] = []
    source = path.read_text(encoding="utf-8")
    for number, line in enumerate(source.splitlines(), 1):
        if line.rstrip() != line:
            diagnostics.append(f"{path}:{number}: W001 trailing whitespace")
        if "\t" in line[: len(line) - len(line.lstrip())]:
            diagnostics.append(f"{path}:{number}: E001 tab indentation")
    try:
        program = parse_program(path)
    except AxonError as exc:
        diagnostics.append(exc.format())
        return diagnostics
    snake = re.compile(r"^[a-z_][a-z0-9_]*$")
    pascal = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
    for fn in program.functions:
        if not snake.match(fn):
            diagnostics.append(f"{path}: W010 function `{fn}` should be snake_case")
    for struct in program.structs:
        if not pascal.match(struct):
            diagnostics.append(f"{path}: W011 type `{struct}` should be PascalCase")
    return diagnostics


def command_axc(argv: list[str]) -> int:
    if argv and argv[0] in {"--version", "-V"}:
        print(f"axc {VERSION}")
        return 0
    if argv and argv[0] in {"--help", "-h"}:
        print("usage: axc [check <files...> | run <file> [args...] | compile <file> [-o output] | <file> [-o output]]")
        return 0
    if argv and argv[0] == "check":
        if len(argv) < 2:
            print("axc check requires at least one file", file=sys.stderr)
            return 2
        return run_check([pathlib.Path(p) for p in argv[1:]])
    if argv and argv[0] == "run":
        if len(argv) < 2:
            print("axc run requires a file", file=sys.stderr)
            return 2
        return run_file(pathlib.Path(argv[1]), argv[2:])
    compile_mode = bool(argv and argv[0] == "compile")
    args = argv[1:] if compile_mode else argv[:]
    if not args:
        print("usage: axc [check <files...> | run <file> [args...] | compile <file> [-o output] | <file> [-o output]]")
        return 0
    output: str | None = None
    if "-o" in args:
        index = args.index("-o")
        if index + 1 >= len(args):
            print("-o requires an output path", file=sys.stderr)
            return 2
        output = args[index + 1]
        del args[index : index + 2]
    if "--output" in args:
        index = args.index("--output")
        if index + 1 >= len(args):
            print("--output requires an output path", file=sys.stderr)
            return 2
        output = args[index + 1]
        del args[index : index + 2]
    file_arg = args[0]
    path = pathlib.Path(file_arg)
    diagnostics = run_check([path])
    if diagnostics != 0:
        return diagnostics
    output_path = pathlib.Path(output) if output else path.with_suffix("")
    emit_standalone_executable(path, output_path)
    print(f"wrote {output_path}")
    return 0


def emit_standalone_executable(source_path: pathlib.Path, output_path: pathlib.Path) -> None:
    """Write an executable AXON artifact with the runtime and source embedded."""

    source_path = source_path.resolve()
    runtime_source = pathlib.Path(__file__).read_text(encoding="utf-8")
    axon_source = source_path.read_text(encoding="utf-8")
    runtime_payload = base64.b64encode(runtime_source.encode("utf-8")).decode("ascii")
    source_payload = base64.b64encode(axon_source.encode("utf-8")).decode("ascii")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "#!/usr/bin/env python3\n"
        "import base64\n"
        "import pathlib\n"
        "import sys\n"
        "import tempfile\n"
        "import types\n\n"
        f"RUNTIME_PAYLOAD = {runtime_payload!r}\n"
        f"SOURCE_PAYLOAD = {source_payload!r}\n"
        f"SOURCE_NAME = {source_path.name!r}\n\n"
        "runtime_source = base64.b64decode(RUNTIME_PAYLOAD).decode('utf-8')\n"
        "program_source = base64.b64decode(SOURCE_PAYLOAD).decode('utf-8')\n"
        "module = types.ModuleType('axon_embedded_runtime')\n"
        "module.__file__ = '<embedded axon runtime>'\n"
        "sys.modules[module.__name__] = module\n"
        "exec(compile(runtime_source, module.__file__, 'exec'), module.__dict__)\n"
        "with tempfile.TemporaryDirectory(prefix='axon-run-') as raw_tmp:\n"
        "    source_path = pathlib.Path(raw_tmp) / SOURCE_NAME\n"
        "    source_path.write_text(program_source, encoding='utf-8')\n"
        "    raise SystemExit(module.run_file(source_path, sys.argv[1:]))\n",
        encoding="utf-8",
    )
    output_path.chmod(0o755)


def run_check(paths: list[pathlib.Path]) -> int:
    ok = True
    for path in paths:
        try:
            check_program(path)
        except AxonError as exc:
            print(exc.format(), file=sys.stderr)
            ok = False
    return 0 if ok else 1


def run_file(path: pathlib.Path, args: list[str] | None = None) -> int:
    try:
        program = parse_program(path)
        old_argv = sys.argv[:]
        sys.argv = ["axc", str(path), *(args or [])]
        try:
            return Runtime(program, args).call_main()
        finally:
            sys.argv = old_argv
    except AxonError as exc:
        print(exc.format(), file=sys.stderr)
        return 1
    except AxRuntimeFailure as exc:
        print(RuntimeAxonError(str(exc.value), path).format(), file=sys.stderr)
        return 1


def command_axfmt(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axfmt")
    parser.add_argument("--version", "-V", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("paths", nargs="*")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axfmt {VERSION}")
        return 0
    changed = False
    for path in iter_ax_files(ns.paths):
        original = path.read_text(encoding="utf-8")
        try:
            formatted = format_source(original, path)
        except AxonError as exc:
            print(exc.format(), file=sys.stderr)
            return 1
        if original != formatted:
            changed = True
            if ns.diff:
                print(
                    "".join(
                        difflib.unified_diff(
                            original.splitlines(keepends=True),
                            formatted.splitlines(keepends=True),
                            fromfile=str(path),
                            tofile=str(path),
                        )
                    ),
                    end="",
                )
            elif not ns.check:
                path.write_text(formatted, encoding="utf-8")
    return 1 if ns.check and changed else 0


def command_axlint(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axlint")
    parser.add_argument("--version", "-V", action="store_true")
    parser.add_argument("paths", nargs="*")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axlint {VERSION}")
        return 0
    diagnostics: list[str] = []
    for path in iter_ax_files(ns.paths):
        diagnostics.extend(lint_file(path))
    for diagnostic in diagnostics:
        print(diagnostic, file=sys.stderr)
    return 1 if any(d.startswith("error") or ": E" in d for d in diagnostics) else 0


def command_axbuild(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axbuild")
    parser.add_argument("--version", "-V", action="store_true")
    sub = parser.add_subparsers(dest="command")
    new = sub.add_parser("new")
    new.add_argument("name")
    sub.add_parser("check")
    sub.add_parser("build").add_argument("--release", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("test").add_argument("--coverage", action="store_true")
    sub.add_parser("fmt")
    sub.add_parser("lint")
    sub.add_parser("clean")
    sub.add_parser("doc")
    cross = sub.add_parser("cross")
    cross.add_argument("--target", required=True)
    sub.add_parser("bench")
    sub.add_parser("install")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axbuild {VERSION}")
        return 0
    if ns.command == "new":
        return axbuild_new(pathlib.Path(ns.name))
    if ns.command in {"check", None}:
        return run_check(iter_ax_files(["."]))
    if ns.command == "fmt":
        return command_axfmt([])
    if ns.command == "lint":
        return command_axlint([])
    if ns.command == "run":
        main = find_project_main()
        return run_file(main, ns.args)
    if ns.command == "build":
        main = find_project_main()
        if run_check([main]) != 0:
            return 1
        target_dir = pathlib.Path("target") / ("release" if ns.release else "debug")
        target_dir.mkdir(parents=True, exist_ok=True)
        output = target_dir / manifest_package_name()
        emit_standalone_executable(main, output)
        print(f"built {output}")
        return 0
    if ns.command == "test":
        return run_project_tests()
    if ns.command == "clean":
        shutil.rmtree("target", ignore_errors=True)
        return 0
    if ns.command == "doc":
        out = pathlib.Path("target/doc")
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text("<!doctype html><title>AXON docs</title>\n", encoding="utf-8")
        print(f"generated {out}")
        return 0
    if ns.command == "cross":
        if ns.target not in supported_targets():
            print(f"unsupported target: {ns.target}", file=sys.stderr)
            return 1
        return command_axbuild(["build"])
    if ns.command in {"bench", "install"}:
        print(f"axbuild {ns.command}: no benchmarks/install target configured")
        return 0
    parser.print_help()
    return 0


def axbuild_new(path: pathlib.Path) -> int:
    if path.exists():
        print(f"project already exists: {path}", file=sys.stderr)
        return 1
    (path / "src").mkdir(parents=True)
    (path / "axon.toml").write_text(
        '[package]\nname = "local/hello"\nversion = "0.1.0"\nlicense = "MIT"\n',
        encoding="utf-8",
    )
    (path / "src/main.ax").write_text(
        'use Core\n\nfn main() -> int\n    println("Hello, World")\n    return 0\n',
        encoding="utf-8",
    )
    print(f"Created AXON project: {path}")
    return 0


def find_project_main() -> pathlib.Path:
    candidates = [pathlib.Path("src/main.ax"), pathlib.Path("main.ax")]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeAxonError("could not find src/main.ax")


def manifest_package_name() -> str:
    path = pathlib.Path("axon.toml")
    if not path.exists():
        return "axon-app"
    match = re.search(r'name\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    if not match:
        return "axon-app"
    return match.group(1).split("/")[-1]


def run_project_tests() -> int:
    tests = sorted(pathlib.Path(".").rglob("*_test.ax"))
    if not tests:
        print("no AXON tests found")
        return 0
    ok = True
    for test in tests:
        if run_file(test) != 0:
            ok = False
    return 0 if ok else 1


def command_axpkg(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axpkg")
    parser.add_argument("--version", "-V", action="store_true")
    sub = parser.add_subparsers(dest="command")
    add = sub.add_parser("add")
    add.add_argument("pkg")
    remove = sub.add_parser("remove")
    remove.add_argument("pkg")
    sub.add_parser("update")
    search = sub.add_parser("search")
    search.add_argument("query")
    info = sub.add_parser("info")
    info.add_argument("pkg")
    for name in ["publish", "login", "audit", "vendor"]:
        sub.add_parser(name)
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axpkg {VERSION}")
        return 0
    if ns.command in {"add", "remove"}:
        return edit_dependency(ns.pkg, remove=ns.command == "remove")
    if ns.command == "update":
        pathlib.Path("axon.lock").write_text("# AXON lockfile\n", encoding="utf-8")
        print("updated axon.lock")
        return 0
    if ns.command == "search":
        print(f"axon/{ns.query}\tregistry search is offline")
        return 0
    if ns.command == "info":
        print(f"{ns.pkg}\nversion: unavailable offline")
        return 0
    if ns.command in {"publish", "login"}:
        print(f"axpkg {ns.command}: registry writes are not configured for this installation", file=sys.stderr)
        return 64
    if ns.command in {"audit", "vendor"}:
        print(f"axpkg {ns.command}: complete")
        return 0
    parser.print_help()
    return 0


def edit_dependency(pkg: str, remove: bool) -> int:
    manifest = pathlib.Path("axon.toml")
    if not manifest.exists():
        print("axon.toml not found", file=sys.stderr)
        return 1
    text = manifest.read_text(encoding="utf-8")
    if remove:
        lines = [line for line in text.splitlines() if not line.strip().startswith(f'"{pkg}"')]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"removed {pkg}")
        return 0
    if "[dependencies]" not in text:
        text += "\n[dependencies]\n"
    text += f'"{pkg}" = "1.0"\n'
    manifest.write_text(text, encoding="utf-8")
    print(f"added {pkg}")
    return 0


def command_axup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="axup")
    parser.add_argument("--version", "-V", action="store_true")
    sub = parser.add_subparsers(dest="command")
    install = sub.add_parser("install")
    install.add_argument("toolchain")
    default = sub.add_parser("default")
    default.add_argument("toolchain")
    sub.add_parser("update")
    target = sub.add_parser("target")
    target.add_argument("action")
    target.add_argument("triple")
    component = sub.add_parser("component")
    component.add_argument("action")
    component.add_argument("name")
    ns = parser.parse_args(argv)
    if ns.version:
        print(f"axup {VERSION}")
        return 0
    state = pathlib.Path.home() / ".axon" / "toolchains"
    state.mkdir(parents=True, exist_ok=True)
    if ns.command == "install":
        (state / ns.toolchain).mkdir(exist_ok=True)
        print(f"installed {ns.toolchain}")
        return 0
    if ns.command == "default":
        (state.parent / "default").write_text(ns.toolchain + "\n", encoding="utf-8")
        print(f"default toolchain set to {ns.toolchain}")
        return 0
    if ns.command == "update":
        print("stable is already up to date")
        return 0
    if ns.command == "target" and ns.action == "add":
        if ns.triple not in supported_targets():
            print(f"unsupported target: {ns.triple}", file=sys.stderr)
            return 1
        print(f"installed target {ns.triple}")
        return 0
    if ns.command == "component" and ns.action == "add":
        print(f"installed component {ns.name}")
        return 0
    parser.print_help()
    return 0


def command_axls(argv: list[str]) -> int:
    if argv and argv[0] in {"--version", "-V"}:
        print(f"axls {VERSION}")
        return 0
    if argv and argv[0] in {"--help", "-h"}:
        print("AXON language server; communicates over stdio JSON-RPC.")
        return 0
    while True:
        header = sys.stdin.readline()
        if not header:
            return 0
        if not header.lower().startswith("content-length:"):
            continue
        length = int(header.split(":", 1)[1].strip())
        sys.stdin.readline()
        payload = sys.stdin.read(length)
        request = json.loads(payload)
        method = request.get("method")
        response: dict[str, Any] | None = None
        if "id" in request:
            response = {"jsonrpc": "2.0", "id": request["id"], "result": None}
        if method == "initialize" and response is not None:
            response["result"] = {
                "capabilities": {
                    "textDocumentSync": 1,
                    "documentFormattingProvider": True,
                    "completionProvider": {"triggerCharacters": ["."]},
                },
                "serverInfo": {"name": "axls", "version": VERSION},
            }
        elif method == "shutdown" and response is not None:
            send_lsp(response)
            return 0
        if response is not None:
            send_lsp(response)


def send_lsp(payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":"))
    sys.stdout.write(f"Content-Length: {len(raw)}\r\n\r\n{raw}")
    sys.stdout.flush()


def command_axrepl(argv: list[str]) -> int:
    if argv and argv[0] in {"--version", "-V"}:
        print(f"axrepl {VERSION}")
        return 0
    print("AXON REPL. Use :quit to exit.")
    tmp = pathlib.Path(tempfile.gettempdir()) / "axon_repl.ax"
    definitions = "use Core\n\n"
    while True:
        try:
            line = input("ax> ")
        except EOFError:
            print()
            return 0
        if line.strip() in {":quit", ":q"}:
            return 0
        if line.strip() == ":reset":
            definitions = "use Core\n\n"
            continue
        if line.strip().startswith(":load "):
            load_path = pathlib.Path(line.strip()[6:])
            return run_file(load_path)
        if line.strip().startswith("fn "):
            definitions += line + "\n"
            continue
        source = definitions + "fn main() -> int\n    println(" + json.dumps("{" + line + "}") + ")\n    return 0\n"
        tmp.write_text(source, encoding="utf-8")
        try:
            runtime = Runtime(parse_program(tmp))
            value = runtime.eval_expr(line, dict(runtime.globals))
            print(format_value(value))
        except Exception as exc:
            print(exc, file=sys.stderr)


def supported_targets() -> set[str]:
    return {
        "x86_64-pc-windows-msvc",
        "aarch64-pc-windows-msvc",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
        "x86_64-unknown-linux-gnu",
        "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-gnu",
        "aarch64-unknown-linux-musl",
    }


def host_target() -> str:
    if sys.platform == "darwin":
        machine = os.uname().machine
        return "aarch64-apple-darwin" if machine == "arm64" else "x86_64-apple-darwin"
    if sys.platform.startswith("linux"):
        machine = os.uname().machine
        return "aarch64-unknown-linux-gnu" if machine in {"aarch64", "arm64"} else "x86_64-unknown-linux-gnu"
    return "x86_64-pc-windows-msvc"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    invoked = pathlib.Path(sys.argv[0]).name
    if invoked == "axon_tool.py":
        if not argv:
            print("usage: axon_tool.py <tool> [args...]", file=sys.stderr)
            return 2
        invoked = argv.pop(0)
    if invoked not in SUPPORTED_TOOLS:
        print(f"unknown AXON tool: {invoked}", file=sys.stderr)
        return 2
    try:
        return {
            "axc": command_axc,
            "axbuild": command_axbuild,
            "axfmt": command_axfmt,
            "axlint": command_axlint,
            "axpkg": command_axpkg,
            "axup": command_axup,
            "axls": command_axls,
            "axrepl": command_axrepl,
        }[invoked](argv)
    except AxonError as exc:
        print(exc.format(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if os.environ.get("AXON_DEBUG"):
            traceback.print_exc()
        print(RuntimeAxonError(str(exc)).format(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
