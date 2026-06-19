"""Runtime value representations shared by the interpreter and standard library."""

from __future__ import annotations

import dataclasses
from typing import Any, Optional


class AxPanic(Exception):
    """An unrecoverable runtime fault (panic / failed assertion)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclasses.dataclass
class AxOption:
    is_some: bool
    value: Any = None

    def __repr__(self) -> str:
        return f"Some({ax_str(self.value)})" if self.is_some else "none"


NONE = AxOption(False)


def some(value: Any) -> AxOption:
    return AxOption(True, value)


@dataclasses.dataclass
class AxResult:
    is_ok: bool
    value: Any = None

    def __repr__(self) -> str:
        tag = "Ok" if self.is_ok else "Err"
        return f"{tag}({ax_str(self.value)})"


def ok(value: Any) -> AxResult:
    return AxResult(True, value)


def err(value: Any) -> AxResult:
    return AxResult(False, value)


@dataclasses.dataclass
class AxStruct:
    type_name: str
    fields: dict[str, Any]

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}: {ax_str(v)}" for k, v in self.fields.items())
        return f"{self.type_name}{{ {inner} }}"


@dataclasses.dataclass
class AxEnum:
    type_name: str
    variant: str
    payload: list[Any]
    field_names: list[Optional[str]] = dataclasses.field(default_factory=list)

    def __repr__(self) -> str:
        if not self.payload:
            return self.variant
        inner = ", ".join(ax_str(v) for v in self.payload)
        return f"{self.variant}({inner})"


@dataclasses.dataclass
class AxRange:
    start: int
    end: int
    inclusive: bool

    def __iter__(self):
        stop = self.end + 1 if self.inclusive else self.end
        return iter(range(self.start, stop))

    def __len__(self) -> int:
        stop = self.end + 1 if self.inclusive else self.end
        return max(0, stop - self.start)


class AxType:
    """A first-class reference to a type or module used for dispatch.

    `args` holds applied generic arguments (e.g. `Channel(str)`), which the
    constructor may consult.
    """

    def __init__(self, name: str, args: tuple = ()) -> None:
        self.name = name
        self.args = args

    def __repr__(self) -> str:
        return self.name


def ax_str(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "none"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e16:
            return f"{value:.1f}"
        return repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, AxOption):
        return repr(value)
    if isinstance(value, AxResult):
        return repr(value)
    if isinstance(value, (AxStruct, AxEnum)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(ax_str(v) for v in value) + "]"
    if isinstance(value, tuple):
        return "(" + ", ".join(ax_str(v) for v in value) + ")"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{ax_str(k)}: {ax_str(v)}" for k, v in value.items()) + "}"
    if isinstance(value, AxRange):
        sep = "..=" if value.inclusive else ".."
        return f"{value.start}{sep}{value.end}"
    return str(value)
