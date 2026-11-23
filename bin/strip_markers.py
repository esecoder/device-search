#!/usr/bin/env python3
"""Strip the markers that make this read like generated code.

Two things are being removed and they need different handling:

  ⚠️ ✅ ❌ 🔴 🟢 🎉 📌     decorative status markers. Removed outright.

  ALL-CAPS SENTENCES       emphasis. Converted to sentence case, because a comment
                           shouting its importance reads as machine-written.

What is NOT removed: the substance. Most of these comments record a measurement or
a failure that cost hours - "measured: 2.58 s per frame", "the first version opened
131 containers inside an HTTP handler". That information is the most useful thing in
the file. It just does not need an alarm emoji in front of it.

Arrows (→ ←) are left alone: they are ordinary typography, not a marker.

    ./.venv/bin/python bin/strip_markers.py --dry-run
    ./.venv/bin/python bin/strip_markers.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXTENSIONS = {".py", ".js", ".css", ".html", ".md", ".sh", ".rs", ".toml", ".json"}

SKIP_DIRS = {"node_modules", ".venv", "target", "dist", "build", ".git",
             "__pycache__", "iconset"}

# Decorations only. Arrows are left out on purpose.
MARKERS = ["⚠️", "⚠", "✅", "❌", "🔴", "🟢", "🎉", "📌", "🚨", "💡", "📊", "🧠"]

# A comment line that is mostly capitals is shouting.
# Two or more capitals, letters only. The earlier pattern required three and
# allowed apostrophes, so it left "IS" and "AS" untouched and turned "match's"
# into "match'S" - both worse than the original.
# Two or more capitals, optionally possessive. The possessive matters: "MATCH'S"
# was lowered as "MATCH" leaving "'S" behind, which produced "match'S" - visibly
# wrong and worse than leaving it alone.


def transform(text: str, path: Path) -> str:
    """Remove the decorative markers and nothing else.

    An earlier version also tried to lower-case the ALL-CAPS prose. It produced
    "why this exists: two questions that look like search", changed "MB" to "mb"
    and "MATCH'S" to "match'S" - worse than the original, because regex cannot do
    English. That pass was reverted.

    What is left is a mechanical, safe transformation: the emoji go, the sentences
    stay exactly as written.
    """
    out = text
    for m in MARKERS:
        out = out.replace(m + " ", "")
        out = out.replace(m, "")

    # A marker at the start of a comment leaves a double space behind.
    out = re.sub(r"(?<=\S)  +(?=\S)", " ", out)

    # Remove the ruled section headers.
    #
    # `===================` lines spanned whole docstrings and comments. They are
    # the most distinctive thing I left behind: nobody writes them by hand, and
    # they appear on almost every module. A blank line separates sections just as
    # well and looks like something a person typed.
    out = re.sub(r"^([ \t]*)(?:[=\-*_#]{6,})[ \t]*$\n?", r"\1\n", out, flags=re.M)

    lines = [ln.rstrip() for ln in out.split("\n")]
    return "\n".join(lines)


def main() -> int:
    dry = "--dry-run" in sys.argv
    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        # Not itself: its MARKERS list contains the characters it removes, so a
        # run would delete the list and the next run would do nothing.
        if p.resolve() == Path(__file__).resolve():
            continue
        files.append(p)

    touched = 0
    for p in files:
        try:
            before = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        after = transform(before, p)
        if after == before:
            continue
        touched += 1
        if dry:
            print(f"  would change {p.relative_to(ROOT)}")
            continue
        # A python file that no longer parses is worse than one with emoji in it.
        if p.suffix == ".py":
            try:
                ast.parse(after)
            except SyntaxError as e:
                print(f"  SKIPPED {p.relative_to(ROOT)}: would not parse ({e.msg})")
                continue
        p.write_text(after, encoding="utf-8")

    print(f"  {touched} files {'would change' if dry else 'changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
