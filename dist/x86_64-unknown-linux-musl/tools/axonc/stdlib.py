"""Built-in standard library for the AXON interpreter.

Provides free functions (`println`, `panic`, ...), method dispatch for builtin
value types (`str`, `Vec`, `Map`, `Option`, `Result`, numbers), and the builtin
type constructors / module statics (`Math`, `Json`, `File`, `Http`, ...).

This is the interpreter-side standard library. The native C backend ships an
equivalent runtime in `runtime.c`.
"""

from __future__ import annotations

import json as _json
import math
import os
import socket
import sys
import urllib.request
from typing import Any

from .values import (
    AxEnum, AxOption, AxPanic, AxRange, AxResult, AxStruct, AxType,
    NONE, ax_str, err, ok, some,
)


# --------------------------------------------------------------------------- #
# Free functions
# --------------------------------------------------------------------------- #
def _println(*args: Any) -> None:
    print(" ".join(ax_str(a) for a in args))


def _print(*args: Any) -> None:
    sys.stdout.write(" ".join(ax_str(a) for a in args))


def _eprintln(*args: Any) -> None:
    print(" ".join(ax_str(a) for a in args), file=sys.stderr)


def _eprint(*args: Any) -> None:
    sys.stderr.write(" ".join(ax_str(a) for a in args))


def _read_line() -> AxOption:
    """Read stdin without its line ending; return None at EOF."""
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except (OSError, UnicodeError) as exc:
        raise AxPanic(f"cannot read stdin: {exc}") from exc
    if line == "":
        return NONE
    if line.endswith("\n"):
        line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
    return some(line)


def _panic(msg: Any = "explicit panic") -> None:
    raise AxPanic(ax_str(msg))


def _assert(cond: Any, msg: Any = "assertion failed") -> None:
    if not _truthy(cond):
        raise AxPanic(ax_str(msg))


def _assert_eq(a: Any, b: Any, msg: Any = None) -> None:
    if not _values_equal(a, b):
        raise AxPanic(ax_str(msg) if msg is not None else f"assert_eq failed: {ax_str(a)} != {ax_str(b)}")


def _truthy(value: Any) -> bool:
    if isinstance(value, AxOption):
        return value.is_some
    return bool(value)


def _values_equal(a: Any, b: Any) -> bool:
    return a == b


BUILTIN_FUNCTIONS = {
    "read_line": _read_line,
    "println": _println,
    "print": _print,
    "eprintln": _eprintln,
    "eprint": _eprint,
    "panic": _panic,
    "assert": _assert,
    "assert_eq": _assert_eq,
    "todo": lambda msg="not yet implemented": _panic(msg),
    "unreachable": lambda msg="entered unreachable code": _panic(msg),
    "unimplemented": lambda msg="not implemented": _panic(msg),
    "type_name": lambda v=None: type(v).__name__,
    "size_of": lambda v=None: 8,
    "len": lambda v: _length(v),
    "max": lambda a, b: a if a >= b else b,
    "min": lambda a, b: a if a <= b else b,
    "abs": lambda a: abs(a),
}


def _length(v: Any) -> int:
    if isinstance(v, AxRange):
        return len(v)
    return len(v)


# --------------------------------------------------------------------------- #
# Builtin instance-method dispatch
# --------------------------------------------------------------------------- #
def builtin_method(interp, recv: Any, name: str, args: list, kwargs: dict) -> Any:
    if isinstance(recv, str):
        return _str_method(recv, name, args, kwargs)
    if isinstance(recv, bool):
        return _bool_method(recv, name, args)
    if isinstance(recv, int):
        return _int_method(recv, name, args)
    if isinstance(recv, float):
        return _float_method(recv, name, args)
    if isinstance(recv, list):
        return _list_method(interp, recv, name, args, kwargs)
    if isinstance(recv, dict):
        return _map_method(recv, name, args, kwargs)
    if isinstance(recv, AxOption):
        return _option_method(interp, recv, name, args)
    if isinstance(recv, AxResult):
        return _result_method(interp, recv, name, args)
    if isinstance(recv, AxRange):
        if name == "len":
            return len(recv)
    if isinstance(recv, _MISC_TYPES):
        return recv.method(interp, name, args, kwargs)
    raise AxPanic(f"value of type {type(recv).__name__} has no method `{name}`")


def _str_method(s: str, name: str, args: list, kwargs: dict) -> Any:
    if name == "len":
        return len(s)
    if name == "is_empty":
        return len(s) == 0
    if name == "trim":
        return s.strip()
    if name == "trim_start":
        return s.lstrip()
    if name == "trim_end":
        return s.rstrip()
    if name == "to_upper" or name == "to_uppercase":
        return s.upper()
    if name == "to_lower" or name == "to_lowercase":
        return s.lower()
    if name == "split":
        sep = args[0] if args else None
        return [p for p in (s.split(sep) if sep is not None else s.split())]
    if name == "lines":
        return s.splitlines()
    if name == "replace":
        return s.replace(args[0], args[1])
    if name == "starts_with":
        return s.startswith(args[0])
    if name == "ends_with":
        return s.endswith(args[0])
    if name == "contains":
        return args[0] in s
    if name == "chars":
        return list(s)
    if name == "bytes":
        return list(s.encode("utf-8"))
    if name == "compare":
        other = args[0]
        return (s > other) - (s < other)
    if name == "slice":
        return _apply_slice(s, args[0])
    if name == "parse":
        return _str_parse(s, args[0] if args else None)
    if name == "repeat":
        return s * int(args[0])
    raise AxPanic(f"str has no method `{name}`")


def _str_parse(s: str, ty: Any) -> Any:
    type_name = ty.name if isinstance(ty, AxType) else str(ty)
    try:
        if type_name.startswith("int") or type_name.startswith("uint") or type_name in {"int", "uint", "byte"}:
            return ok(int(s.strip()))
        if type_name.startswith("float"):
            return ok(float(s.strip()))
        if type_name == "bool":
            return ok(s.strip() == "true")
        return ok(s)
    except ValueError:
        return err(f"cannot parse '{s}' as {type_name}")


def _bool_method(b: bool, name: str, args: list) -> Any:
    if name == "to_string":
        return "true" if b else "false"
    raise AxPanic(f"bool has no method `{name}`")


def _int_method(n: int, name: str, args: list) -> Any:
    if name == "to_string":
        return str(n)
    if name == "max":
        return max(n, args[0])
    if name == "min":
        return min(n, args[0])
    if name == "abs":
        return abs(n)
    if name == "clamp":
        return max(args[0], min(n, args[1]))
    if name == "pow":
        return n ** int(args[0])
    if name == "to_float":
        return float(n)
    raise AxPanic(f"int has no method `{name}`")


def _float_method(x: float, name: str, args: list) -> Any:
    if name == "to_string":
        return ax_str(x)
    if name == "floor":
        return math.floor(x)
    if name == "ceil":
        return math.ceil(x)
    if name == "round":
        return round(x)
    if name == "sqrt":
        return math.sqrt(x)
    if name == "abs":
        return abs(x)
    if name == "max":
        return max(x, args[0])
    if name == "min":
        return min(x, args[0])
    if name == "to_int":
        return int(x)
    raise AxPanic(f"float has no method `{name}`")


def _list_method(interp, lst: list, name: str, args: list, kwargs: dict) -> Any:
    if name == "len":
        return len(lst)
    if name == "is_empty":
        return len(lst) == 0
    if name == "push":
        lst.append(args[0]); return None
    if name == "pop":
        return some(lst.pop()) if lst else NONE
    if name == "first":
        return some(lst[0]) if lst else NONE
    if name == "last":
        return some(lst[-1]) if lst else NONE
    if name == "get":
        i = int(args[0])
        return some(lst[i]) if 0 <= i < len(lst) else NONE
    if name == "contains":
        return args[0] in lst
    if name == "slice":
        return _apply_slice(lst, args[0])
    if name == "to_vec":
        return list(lst)
    if name == "reverse":
        return list(reversed(lst))
    if name == "sort":
        return sorted(lst)
    if name == "map":
        return [interp.call_value(args[0], [x], {}) for x in lst]
    if name == "filter":
        return [x for x in lst if _truthy(interp.call_value(args[0], [x], {}))]
    if name == "collect":
        return list(lst)
    if name == "sum":
        return sum(lst)
    if name == "join":
        return (args[0] if args else "").join(ax_str(x) for x in lst)
    if name == "sort_by":
        import functools
        cmp = args[0]
        return sorted(lst, key=functools.cmp_to_key(lambda a, b: interp.call_value(cmp, [a, b], {})))
    raise AxPanic(f"Vec has no method `{name}`")


def _map_method(d: dict, name: str, args: list, kwargs: dict) -> Any:
    if name == "len":
        return len(d)
    if name == "is_empty":
        return len(d) == 0
    if name == "get" or name == "get_mut":
        key = args[0]
        return some(d[key]) if key in d else NONE
    if name == "insert":
        d[args[0]] = args[1]; return None
    if name == "remove":
        if args[0] in d:
            return some(d.pop(args[0]))
        return NONE
    if name == "contains_key":
        return args[0] in d
    if name == "keys":
        return list(d.keys())
    if name == "values":
        return list(d.values())
    if name == "entries":
        return [(k, v) for k, v in d.items()]
    raise AxPanic(f"Map has no method `{name}`")


def _option_method(interp, opt: AxOption, name: str, args: list) -> Any:
    if name == "is_some":
        return opt.is_some
    if name == "is_none":
        return not opt.is_some
    if name == "unwrap":
        if opt.is_some:
            return opt.value
        raise AxPanic("called unwrap on a none value")
    if name == "unwrap_or":
        return opt.value if opt.is_some else args[0]
    if name == "unwrap_or_else":
        return opt.value if opt.is_some else interp.call_value(args[0], [], {})
    if name == "map":
        return some(interp.call_value(args[0], [opt.value], {})) if opt.is_some else NONE
    if name == "expect":
        if opt.is_some:
            return opt.value
        raise AxPanic(ax_str(args[0]) if args else "expectation failed")
    raise AxPanic(f"Option has no method `{name}`")


def _result_method(interp, res: AxResult, name: str, args: list) -> Any:
    if name == "is_ok":
        return res.is_ok
    if name == "is_err":
        return not res.is_ok
    if name == "unwrap":
        if res.is_ok:
            return res.value
        raise AxPanic(f"called unwrap on an Err value: {ax_str(res.value)}")
    if name == "unwrap_or":
        return res.value if res.is_ok else args[0]
    if name == "unwrap_err":
        if not res.is_ok:
            return res.value
        raise AxPanic("called unwrap_err on an Ok value")
    if name == "ok":
        return some(res.value) if res.is_ok else NONE
    if name == "map":
        return ok(interp.call_value(args[0], [res.value], {})) if res.is_ok else res
    raise AxPanic(f"Result has no method `{name}`")


def _apply_slice(seq, index):
    if isinstance(index, AxRange):
        stop = index.end + 1 if index.inclusive else index.end
        return seq[index.start:stop]
    return seq[index]


# --------------------------------------------------------------------------- #
# Builtin type constructors / module statics
# --------------------------------------------------------------------------- #
def builtin_static(interp, ty: AxType, name: str, args: list, kwargs: dict) -> Any:
    n = ty.name
    if n == "Math":
        return _math_static(name, args)
    if n == "Json" or n == "json":
        return _json_static(interp, name, args, kwargs)
    if n == "File":
        return _file_static(name, args)
    if n == "HttpClient":
        if name == "new":
            return HttpClient()
    if n == "TcpListener":
        if name == "bind":
            return TcpListener.bind(args[0])
    if n == "App":
        if name == "new":
            return CliApp(kwargs.get("name", args[0] if args else "app"),
                          kwargs.get("version", args[1] if len(args) > 1 else "0.0.0"))
    if n in {"Channel", "Vec", "Map", "Arc", "Mutex", "RwLock", "Rc"}:
        if name == "new":
            return construct_builtin(interp, ty, args, kwargs)
    if name == "new":
        return construct_builtin(interp, ty, args, kwargs)
    raise AxPanic(f"type `{n}` has no static member `{name}`")


def builtin_static_field(interp, ty: AxType, name: str) -> Any:
    if ty.name == "Math":
        consts = {"PI": math.pi, "E": math.e, "TAU": math.tau, "INF": math.inf}
        if name in consts:
            return consts[name]
    raise AxPanic(f"type `{ty.name}` has no member `{name}`")


def _math_static(name: str, args: list) -> Any:
    fns = {
        "sqrt": math.sqrt, "abs": abs, "floor": math.floor, "ceil": math.ceil,
        "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
        "exp": math.exp, "pow": lambda b, e: b ** e,
        "max": max, "min": min, "round": round,
    }
    if name in fns:
        return fns[name](*args)
    raise AxPanic(f"Math has no member `{name}`")


def _json_static(interp, name: str, args: list, kwargs: dict) -> Any:
    if name == "parse":
        try:
            return ok(_wrap_json(_json.loads(args[0])))
        except Exception as exc:
            return err(f"invalid json: {exc}")
    if name == "stringify":
        return _json.dumps(_unwrap_json(args[0]))
    if name == "parse_as":
        type_ref = args[0]

        def parser(text: str):
            try:
                raw = _json.loads(text)
            except Exception as exc:
                return err(f"invalid json: {exc}")
            return ok(interp.make_struct_from_dict(type_ref.name, raw))

        return parser
    raise AxPanic(f"Json has no member `{name}`")


def _wrap_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _wrap_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wrap_json(v) for v in value]
    return value


def _unwrap_json(value: Any) -> Any:
    if isinstance(value, AxStruct):
        return {k: _unwrap_json(v) for k, v in value.fields.items()}
    if isinstance(value, dict):
        return {k: _unwrap_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_json(v) for v in value]
    if isinstance(value, AxOption):
        return _unwrap_json(value.value) if value.is_some else None
    return value


def _file_static(name: str, args: list) -> Any:
    if name == "exists":
        return os.path.exists(args[0])
    if name == "open":
        path = args[0]
        if not os.path.exists(path):
            raise AxPanic(f"file not found: {path}")
        return AxFile(path)
    if name == "read_to_string":
        try:
            with open(args[0], "r", encoding="utf-8") as fh:
                return ok(fh.read())
        except OSError:
            return err("cannot read file")
    if name == "write":
        with open(args[0], "w", encoding="utf-8") as fh:
            fh.write(args[1])
        return ok(True)
    raise AxPanic(f"File has no member `{name}`")


def construct_builtin(interp, ty: AxType, args: list, kwargs: dict) -> Any:
    n = ty.name
    if n == "Channel":
        return Channel(int(kwargs.get("buffer", args[0] if args else 0)))
    if n in {"Vec"}:
        return list(args[0]) if args else []
    if n in {"Map"}:
        return dict(args[0]) if args else {}
    if n in {"Arc", "Rc", "Mutex", "RwLock"}:
        return args[0] if args else None
    raise AxPanic(f"cannot construct `{n}`")


# --------------------------------------------------------------------------- #
# Builtin object types
# --------------------------------------------------------------------------- #
class AxFile:
    def __init__(self, path: str) -> None:
        self.path = path

    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "read_all_text" or name == "read_to_string":
            with open(self.path, "r", encoding="utf-8") as fh:
                return fh.read()
        if name == "read_lines":
            with open(self.path, "r", encoding="utf-8") as fh:
                return fh.read().splitlines()
        if name == "write_all" or name == "write":
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(ax_str(args[0]))
            return None
        raise AxPanic(f"File has no method `{name}`")


class HttpClient:
    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "get":
            try:
                with urllib.request.urlopen(args[0], timeout=15) as resp:
                    return HttpResponse(int(resp.status), resp.read().decode("utf-8", "replace"))
            except Exception as exc:
                raise AxPanic(f"http get failed: {exc}")
        raise AxPanic(f"HttpClient has no method `{name}`")


class HttpResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body

    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "text":
            return self.body
        if name == "json":
            return ok(_wrap_json(_json.loads(self.body)))
        raise AxPanic(f"HttpResponse has no method `{name}`")

    def field(self, name: str) -> Any:
        if name == "status":
            return self.status
        if name == "body":
            return self.body
        raise AxPanic(f"HttpResponse has no field `{name}`")


class Channel:
    def __init__(self, buffer: int = 0) -> None:
        from collections import deque
        self.items = deque()
        self.buffer = buffer

    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "send":
            self.items.append(args[0]); return None
        if name == "recv":
            if not self.items:
                raise AxPanic("channel receive on empty channel")
            return self.items.popleft()
        if name == "try_recv":
            return some(self.items.popleft()) if self.items else NONE
        if name == "len":
            return len(self.items)
        raise AxPanic(f"Channel has no method `{name}`")


class TcpListener:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    @staticmethod
    def bind(address: str) -> "TcpListener":
        host, _, port = address.rpartition(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        sock.listen()
        return TcpListener(sock)

    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "accept":
            conn, _ = self.sock.accept()
            return TcpStream(conn)
        raise AxPanic(f"TcpListener has no method `{name}`")


class TcpStream:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = b""

    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "read":
            data = self.sock.recv(4096)
            self._buf = data
            return len(data)
        if name == "write_all" or name == "write":
            payload = args[0]
            if isinstance(payload, list):
                data = bytes(payload)
            elif isinstance(payload, (bytes, bytearray)):
                data = bytes(payload)
            else:
                data = ax_str(payload).encode("utf-8")
            self.sock.sendall(data)
            return None
        raise AxPanic(f"TcpStream has no method `{name}`")


class CliApp:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self.args: list[str] = []

    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "arg":
            self.args.append(args[0] if args else kwargs.get("name", ""))
            return self
        if name == "parse":
            return CliMatches(sys.argv[2:])
        raise AxPanic(f"App has no method `{name}`")


class CliMatches:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv

    def method(self, interp, name: str, args: list, kwargs: dict) -> Any:
        if name == "value":
            if not self.argv:
                raise AxPanic(f"missing required argument: {args[0] if args else ''}")
            return self.argv[0]
        if name == "values":
            return list(self.argv)
        raise AxPanic(f"CliMatches has no method `{name}`")


_MISC_TYPES = (
    AxFile, HttpClient, HttpResponse, Channel, TcpListener, TcpStream,
    CliApp, CliMatches,
)


def builtin_object_field(recv: Any, name: str) -> Any:
    if isinstance(recv, HttpResponse):
        return recv.field(name)
    raise AxPanic(f"value of type {type(recv).__name__} has no field `{name}`")
