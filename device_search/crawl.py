"""
crawl.py — walk the filesystem and turn files into text.

⚠️ THIS IS THE PART THAT TOUCHES EVERYTHING, so it is written defensively:

    * a permission error is a SKIP, not a crash  — half of `~/Library` is unreadable
    * a symlink loop is detected, not followed forever
    * a file that vanishes mid-walk (a build writing output) is a skip
    * a 4 GB disk image is rejected on SIZE before it is ever opened

⚠️ And every skip is COUNTED AND REPORTED. A crawler that silently drops files produces a
search tool whose misses are indistinguishable from "not on disk" — which is the worst possible
failure for a tool whose entire job is telling you whether something exists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import (MAX_FILE_BYTES, MAX_LINE_BYTES, SKIP_DIR_NAMES, is_text_file, lang_of)


@dataclass
class CrawlStats:
    """⚠️ Every number here is printed at the end of a crawl. Nothing is silent."""
    seen: int = 0
    indexed: int = 0
    skipped: dict = field(default_factory=dict)
    bytes_indexed: int = 0
    errors: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def report(self) -> str:
        lines = [
            f"  files seen      : {self.seen:,}",
            f"  indexed         : {self.indexed:,}  ({self.bytes_indexed/1e6:.1f} MB of text)",
        ]
        if self.skipped:
            lines.append("  skipped:")
            for reason, n in sorted(self.skipped.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {reason:<20} {n:,}")
        if self.errors:
            lines.append(f"  read errors     : {self.errors:,}  (permissions / vanished files)")
        return "\n".join(lines)


@dataclass
class FileDoc:
    path: str
    mtime: float
    size: int
    lang: str
    text: str
    n_lines: int


def _read_text(path: Path) -> str | None:
    """Read a file as text, or return None.

    ⚠️ `errors="replace"` IS DELIBERATE. Plenty of real source files are latin-1 or have a
    stray invalid byte, and refusing to index them because of one byte loses the whole file.
    The replacement character is harmless for search, and it is honest: the text really is
    not valid utf-8.

    ⚠️ And the LINE-LENGTH CAP matters more than it looks: a minified JS bundle is one line of
    2 MB. Indexing it whole makes the store huge and every line-number report meaningless, so
    very long lines are truncated with a marker.
    """
    try:
        raw = path.read_bytes()
    except (OSError, PermissionError):
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    if len(text) > MAX_FILE_BYTES:
        text = text[:MAX_FILE_BYTES]
    out = []
    for line in text.splitlines():
        if len(line) > MAX_LINE_BYTES:
            out.append(line[:MAX_LINE_BYTES] + " …[line truncated]")
        else:
            out.append(line)
    return "\n".join(out)


def walk(roots: list[Path], include_deps: bool = False, progress_every: int = 20000):
    """Yield FileDoc for every indexable file under `roots`.

    ⚠️ `os.walk(followlinks=False)` IS THE DEFAULT AND MUST STAY THAT WAY. Following symlinks
    on a home directory is the classic way to walk `/` through a link in a project folder and
    never finish. Directories that ARE symlinks are still descended once because `os.walk`
    lists them in `dirnames`; we filter them below.

    ⚠️ THE IN-PLACE `dirnames[:]` FILTER is what makes this fast: pruning `node_modules` here
    means the walk never stats its 40,000 files at all.
    """
    stats = CrawlStats()
    for root in roots:
        root = Path(root)
        if not root.exists():
            stats.skip("root_missing")
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            d = Path(dirpath)
            # Prune directories we will never want. Note: NOT a privacy exclusion —
            # `--include-deps` re-enables them.
            keep = []
            for name in dirnames:
                if name in SKIP_DIR_NAMES and not include_deps:
                    stats.skip(f"dir:{name}")
                    continue
                if name.startswith(".") and name in SKIP_DIR_NAMES:
                    stats.skip(f"dir:{name}")
                    continue
                # ⚠️ Drop symlinked dirs so we cannot loop.
                if (d / name).is_symlink():
                    stats.skip("symlink_dir")
                    continue
                keep.append(name)
            dirnames[:] = keep

            for fname in filenames:
                stats.seen += 1
                if stats.seen % progress_every == 0:
                    print(f"    … {stats.seen:,} seen, {stats.indexed:,} indexed", flush=True)
                p = d / fname
                try:
                    st = p.stat()
                except (OSError, PermissionError):
                    stats.skip("stat_failed")
                    continue
                if not p.is_file():
                    stats.skip("not_regular_file")
                    continue
                ok, reason = is_text_file(p, st.st_size)
                if not ok:
                    stats.skip(reason)
                    continue
                text = _read_text(p)
                if text is None:
                    stats.errors += 1
                    stats.skip("read_failed")
                    continue
                if not text.strip():
                    stats.skip("empty")
                    continue
                stats.indexed += 1
                stats.bytes_indexed += len(text)
                yield FileDoc(str(p), st.st_mtime, st.st_size, lang_of(p), text,
                              text.count("\n") + 1)
    # ⚠️ Stats are attached to the generator so the caller can print them after the loop.
    walk.stats = stats


def find_line(text: str, needle: str) -> tuple[int, str] | None:
    """Locate `needle` in `text`, returning (1-based line number, that line).

    ⚠️ USED FOR PROVENANCE, NOT FOR SEARCHING. The search backends already know which file
    matched; this turns "matched this file" into "matched line 214", which is the difference
    between a useful result and a `grep`-shaped shrug. Case-insensitive on the fallback so a
    semantic hit still gets a line number even when the words differ.
    """
    low = text.lower()
    n = needle.lower().strip()
    if not n:
        return None
    idx = low.find(n)
    if idx == -1:
        # Try the longest token, which is the most discriminating part of most queries.
        toks = sorted((t for t in n.split() if len(t) > 3), key=len, reverse=True)
        for t in toks:
            idx = low.find(t)
            if idx != -1:
                break
        else:
            return None
    line_no = text.count("\n", 0, idx) + 1
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    if end == -1:
        end = min(len(text), start + 400)
    return line_no, text[start:end][:400]
