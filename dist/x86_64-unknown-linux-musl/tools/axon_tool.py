#!/usr/bin/env python3
"""AXON toolchain entry point.

Dispatches the multi-call binary (`axc`, `axbuild`, `axfmt`, `axlint`, `axpkg`,
`axup`, `axls`, `axrepl`) to the AXON compiler implemented in the `axonc`
package next to this file. Keeping a single entry point lets the distribution
wrappers stay tiny shell scripts.
"""

from __future__ import annotations

import os
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from axonc import VERSION  # noqa: E402
from axonc import cli  # noqa: E402
from axonc.errors import Diagnostic  # noqa: E402

SUPPORTED_TOOLS = {
    "axc": cli.command_axc,
    "axbuild": cli.command_axbuild,
    "axfmt": cli.command_axfmt,
    "axlint": cli.command_axlint,
    "axpkg": cli.command_axpkg,
    "axup": cli.command_axup,
    "axls": cli.command_axls,
    "axrepl": cli.command_axrepl,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    invoked = pathlib.Path(sys.argv[0]).name
    if invoked in {"axon_tool.py", "axon_tool", "__main__.py", "python3", "python"}:
        if not argv:
            print(f"AXON {VERSION}\nusage: axon_tool.py <tool> [args...]", file=sys.stderr)
            return 2
        invoked = argv.pop(0)
    if invoked not in SUPPORTED_TOOLS:
        print(f"unknown AXON tool: {invoked}", file=sys.stderr)
        return 2
    try:
        return SUPPORTED_TOOLS[invoked](argv)
    except Diagnostic as exc:
        print(exc.render(sys.stderr.isatty()), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # pragma: no cover - last-resort guard
        if os.environ.get("AXON_DEBUG"):
            traceback.print_exc()
        print(f"axon: internal error[E999]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
