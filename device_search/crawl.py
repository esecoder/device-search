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

from .config import (MAX_FILE_BYTES, MAX_LINE_BYTES, MEDIA_EXTS, SKIP_DIR_NAMES,
                     is_text_file, lang_of)
from .extract import extract


@dataclass
class CrawlStats:
    """⚠️ Every number here is printed at the end of a crawl. Nothing is silent."""
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0       # ⚠️ skipped because mtime+size matched the index
    media_seen: int = 0      # ⚠️ PDFs and images that went through extract()
    media_indexed: int = 0   # ⚠️ of those, how many actually contained text
    skipped: dict = field(default_factory=dict)
    bytes_indexed: int = 0
    errors: int = 0

    # ⚠️ THE PATHS, NOT JUST THE COUNTS. A count tells the user something was skipped; the path
    # is what lets them find it by name and grep it on demand. Without this the information is
    # unactionable, which is barely better than not having it.
    missing: list = field(default_factory=list)

    def skip(self, reason: str, path: str = "", size: int = 0) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        # ⚠️ ONLY THE REASONS A USER CAN ACT ON. `unchanged` is a success (the file is already
        # indexed) and `empty` is a property of the file, not a failure to index it. Listing them
        # would bury the 150 that actually matter under thousands that do not.
        if path and reason in ("too_large", "read_failed", "media_no_text", "no_text_content"):
            self.missing.append((path, size, reason))

    def report(self) -> str:
        lines = [
            f"  files seen      : {self.seen:,}",
            f"  unchanged       : {self.unchanged:,}  (skipped — mtime+size match the index)",
            f"  re-indexed      : {self.indexed:,}  ({self.bytes_indexed/1e6:.1f} MB of text)",
        ]
        if self.media_seen:
            # ⚠️ Media is reported SEPARATELY and always, once any was seen. A PDF-heavy or
            # photo-heavy corpus otherwise looks identical to one where media was skipped.
            lines.append(f"  media seen      : {self.media_seen:,}  "
                         f"({self.media_indexed:,} contained text, "
                         f"{self.media_seen - self.media_indexed:,} did not)")
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
    # ⚠️ HOW the text was obtained: "utf8", "pdftext", "ocr". This travels into the index and
    # into search results, because "found in scan.pdf" and "found in scan.pdf via OCR" are
    # different claims about reliability and the user is entitled to know which one they got.
    method: str = "utf8"


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


def walk(roots: list[Path], include_deps: bool = False, progress_every: int = 20000,
         known: dict | None = None):
    """Yield FileDoc for every indexable file under `roots`.

    ⚠️⚠️ `known` IS THE INCREMENTAL RE-INDEX, AND IT IS THE MOST IMPORTANT PARAMETER HERE.
    It maps path -> (mtime, size) from the existing index. A file whose mtime AND size are
    unchanged is SKIPPED WITHOUT BEING OPENED.

    ⚠️ WHY THIS IS NOT AN OPTIMISATION BUT A PREREQUISITE: `stat` costs microseconds; reading,
    extracting and embedding costs seconds. Without this, every run is a full rebuild — which
    for a text corpus is minutes, and for OCR over 66k images is 18-36 HOURS. An app you cannot
    re-run is an app you run once, and then it silently stops finding new files.

    ⚠️ AND WHY SIZE IS CHECKED TOO, NOT JUST MTIME: mtime LIES. `rsync -t`, archivers and some
    editors preserve an old modification time on new content, so an mtime-only check would
    serve a stale index forever. Size catches nearly all of those for one extra integer
    comparison. (A content hash would be exact, but it requires reading the file — which is the
    cost we are trying to avoid.)

    ⚠️ `os.walk(followlinks=False)` IS THE DEFAULT AND MUST STAY THAT WAY. Following symlinks
    on a home directory is the classic way to walk `/` through a link in a project folder and
    never finish. Directories that ARE symlinks are still descended once because `os.walk`
    lists them in `dirnames`; we filter them below.

    ⚠️ THE IN-PLACE `dirnames[:]` FILTER is what makes this fast: pruning `node_modules` here
    means the walk never stats its 40,000 files at all.
    """
    if known is None:
        known = {}
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

                # ⚠️ THE INCREMENTAL CHECK. It must come AFTER the file-type check (so a
                # binary file is still counted as skipped for the right reason) and BEFORE
                # `is_text_file` / `_read_text` (which is where the cost lives).
                prev = known.get(str(p))
                if prev is not None and prev[0] == st.st_mtime and prev[1] == st.st_size:
                    stats.unchanged += 1
                    continue

                ok, reason = is_text_file(p, st.st_size)
                if not ok:
                    # ⚠️ `too_large` AND `binary_ext` CARRY THE PATH. The first is the one the
                    # user most needs to know about: the file is real, searchable in principle,
                    # and invisible. The second is how you find out that a 200 MB binary exists.
                    stats.skip(reason, str(p), sz)
                    continue
                ext = p.suffix.lower()
                if ext in MEDIA_EXTS:
                    # ⚠️ MEDIA GOES THROUGH extract(), WHICH IS SLOW (OCR is ~1-2s a file).
                    # ⚠️ AND AN IMAGE WITH NO TEXT IS NOT AN ERROR — it is the common case for
                    # photos, and it gets its own counter so a user can see the difference
                    # between "not indexed" and "indexed, found no text".
                    text, method = extract(p)
                    stats.media_seen += 1
                    if not text.strip():
                        stats.skip("media_no_text")
                        continue
                    stats.media_indexed += 1
                else:
                    text = _read_text(p)
                    method = "utf8"
                    if text is None:
                        stats.errors += 1
                        stats.skip("read_failed")
                        continue
                    if not text.strip():
                        stats.skip("empty")
                        continue
                stats.indexed += 1
                stats.bytes_indexed += len(text)
                yield FileDoc(str(p), st.st_mtime, st.st_size,
                              "ocr" if method == "ocr" else lang_of(p), text,
                              text.count("\n") + 1, method)
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
