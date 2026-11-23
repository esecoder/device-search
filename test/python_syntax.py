#!/usr/bin/env python3
"""Every Python file must parse, before any test claims the code works.

This exists because a commit went in with a syntax error in server.py. The test
suite passed, the commit was made, and the daemon could not have started — the
tests exercise the browser-side setup panel with `window.eval`, and never import
the server module. A suite that does not read the code it is validating will
report success on a file that cannot be imported.

`ast.parse` is enough here: it catches syntax errors without importing, so a
missing dependency or a side effect at import time cannot hide the result.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    files = sorted(
        p for p in (ROOT / "device_search").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    if not files:
        print(" FAIL no Python files found")
        return 1

    failures = []
    for path in files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            failures.append((path, exc))

    if failures:
        for path, exc in failures:
            rel = path.relative_to(ROOT)
            print(f" FAIL {rel}:{exc.lineno}: {exc.msg}")
        print(f"\n {len(failures)} of {len(files)} files do not parse")
        return 1

    print(f" PASS all {len(files)} Python files parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
