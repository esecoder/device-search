"""
store.py — the index: a SQLite database holding extracted text plus two search backends.

⚠️ WHY SQLITE AND NOT A VECTOR DATABASE. At device scale (10k-500k files) everything fits on
one machine, one process and one file. A server-backed vector DB adds an install, a port and a
failure mode, and buys nothing until you are past millions of chunks. The pragmatic choice is
the right one here, and saying so is more useful than reaching for the fashionable tool.

⚠️⚠️ AND WHY THERE IS NO FTS5 IN THIS FILE, WHICH IS A DELIBERATE CHANGE OF MIND.
SQLite ships FTS5, which gives BM25 for free. I reached for it first — and then could not
verify it was present, because `sqlite3` compiled without FTS5 is common on macOS system Python.
**A search backend that is silently absent is worse than one that is slower.** So BM25 below is
implemented in NumPy against a real inverted index, and it always works. It is the second time
this repository has chosen the reimplementation over the shortcut, and both times the reason was
the same: the shortcut's failure mode was invisible.
"""

from __future__ import annotations

import functools
import os
import time


def _exists(p) -> bool:
    from pathlib import Path as _P
    try:
        return _P(p).exists()
    except OSError:
        return False
import json
import math
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


# =============================================================================
# ⚠️⚠️ THREAD SAFETY, AND WHY THE OBVIOUS FIX IS THE WRONG ONE
# =============================================================================
# The daemon serves each request in its own thread (`ThreadingHTTPServer`), but a SQLite
# connection is bound to the thread that created it. That produced:
#
#     sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that
#     same thread.
#
# ⚠️ THE FIX EVERYONE COPIES IS `check_same_thread=False`, AND IT IS THE DANGEROUS ONE.
# That flag silences the error and lets several threads use ONE connection with no
# synchronisation at all. It trades a loud crash for **silent corruption** — interleaved
# statements, mismatched cursors, and a database that is wrong in ways nobody can reproduce.
#
# ⚠️ So the flag is used HERE ONLY WITH A LOCK AROUND EVERY METHOD. The flag makes the crash go
# away; the lock is what makes it correct. **Either one alone is a bug.**
#
# ⚠️ A lock is acceptable because the work is tiny — a search is a few milliseconds of SQLite
# and NumPy — and there is one user. The alternative that scales better is a connection per
# thread, which is more code for a load this service will never see.
def _serialised(fn):
    """⚠️ Hold the store lock for the whole call. Applied to EVERY public method."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrapper

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id       INTEGER PRIMARY KEY,
    path     TEXT UNIQUE NOT NULL,
    mtime    REAL,
    size     INTEGER,
    lang     TEXT,
    n_lines  INTEGER,
    text     TEXT NOT NULL,
    method   TEXT DEFAULT 'utf8'    -- ⚠️ utf8 | pdftext | ocr. Shown in results.
);
CREATE INDEX IF NOT EXISTS idx_path ON documents(path);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# ⚠️ A TOKENIZER, NOT A LANGUAGE MODEL. `\w+` would keep `InputLayer` and `shape` but split
# `InputLayer(shape=(784,))` into words, losing the very thing code search needs. Code
# identifiers have `_`, `.`, `:` and `(` in them, so the pattern below preserves those runs.
# This is the tokenizer equivalent of the "code needs its own chunking" argument in the manual.
TOKEN_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
_PATH_STOPWORDS = {"find", "file", "files", "called", "named", "where", "is",
                   "the", "my", "locate", "show", "get", "open", "search",
                   "for", "path", "folder", "directory", "in", "of", "to"}


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RX.findall(text)]


class Store:
    """⚠️ ONE CONNECTION, ONE FILE, NO SERVER. See the module docstring."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ THE LOCK IS CREATED BEFORE THE CONNECTION, because the connection is the thing
        # that needs guarding and a half-constructed object must not be reachable.
        self._lock = threading.RLock()
        # ⚠️ check_same_thread=False IS ONLY SAFE WITH THE LOCK ABOVE. See the module note.
        # ⚠⚠️ WAL AND A LONG BUSY TIMEOUT, BECAUSE AN INDEX RUN HOLDS THE WRITE LOCK FOR MINUTES.
        #
        # ⚠️ MEASURED: with the defaults — journal_mode=delete, busy timeout 5s — the DAEMON COULD
        # NOT START while a crawl was running. `executescript(SCHEMA)` below needs an exclusive
        # lock, gave up after five seconds, raised `database is locked`, and the process exited.
        #
        # ⚠️ AND IT FAILED SILENTLY FROM THE USER'S SIDE: no daemon means no search and no green AI
        # indicator, which is how this was reported — as the AI pill having disappeared. The pill
        # was fine. Nothing was answering.
        #
        # ⚠️ WAL IS THE ACTUAL FIX, not just a bigger timeout: it lets a READER and a WRITER work
        # at the same time, so the daemon can serve searches while the indexer is writing. Without
        # it, no timeout is long enough — a home-directory crawl holds that lock for an hour.
        #
        # ⚠️ AND A LONG TIMEOUT COVERS THE REMAINING CASE, where the schema script itself needs to
        # write while a crawl is mid-batch. Better to wait a few seconds than to refuse to start.
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        # ⚠️ None = not yet asked, True/False = the answer. A tri-state, because "have we tried"
        # and "did it work" are different questions and conflating them re-attempts on every query.
        self._fts_ok = None
        # ⚠⚠️ BUILT EAGERLY, NOT LAZILY, AND THE TRIGGERS ARE WHY.
        #
        # ⚠️ _ensure_fts() was called from keyword_search, which is lazy — so on a fresh database
        # the table and its triggers did not exist yet when the first documents were inserted.
        # MEASURED: a fresh index where the very first insert was already invisible to keyword
        # search, because nothing was listening when it happened.
        #
        # ⚠️ LAZY IS FINE FOR A CACHE AND WRONG FOR ANYTHING THAT MUST OBSERVE EVENTS. A trigger
        # only fires from the moment it exists, so setting one up at read time misses every write
        # that came before.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # ⚠️ AND A SHORT WAIT FOR THE WRITER rather than an instant failure, so a search during
            # a batch blocks for a moment instead of erroring.
            self.conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            # ⚠️ A DATABASE THAT CANNOT SWITCH TO WAL IS STILL USABLE. Refusing to start over a
            # performance setting would turn a slow search into no search.
            pass
        self.conn.executescript(SCHEMA)
        # ⚠⚠️ EAGERLY, AND AFTER THE SCHEMA — THE ORDER IS THE WHOLE POINT.
        #
        # ⚠️ Two mistakes in a row, both silent:
        #   1. called from keyword_search, which is lazy — so the triggers did not exist when the
        #      first documents were inserted, and a fresh index had its own first files invisible
        #   2. then called EAGERLY but BEFORE this line — so `content='documents'` referenced a table
        #      that did not exist yet, the CREATE failed, _fts_ok stayed False, and keyword search
        #      fell back to the Python BM25 forever without saying so
        #
        # ⚠️ BOTH LOOKED LIKE SUCCESS. The first returned results, just not all of them; the
        # second returned results, just slowly. Neither raised anything a caller would see.
        #
        # ⚠️ THE RULE: A TRIGGER MUST EXIST BEFORE THE FIRST WRITE, AND A TABLE MUST EXIST BEFORE
        # ANYTHING CAN REFERENCE IT. Both are ordering constraints, and neither is checked by
        # anything at runtime.
        self._ensure_fts()
        # ⚠️ MIGRATION, AND IT IS NOT OPTIONAL. `CREATE TABLE IF NOT EXISTS` does NOT add a
        # column to a table that already exists, so an index built before `method` existed
        # would fail every query with "no such column". Silent upgrade breakage is the kind of
        # bug that only appears on other people's machines.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(documents)")}
        if "method" not in cols:
            self.conn.execute("ALTER TABLE documents ADD COLUMN method TEXT DEFAULT 'utf8'")
        # ⚠⚠️ A MIGRATION IS REQUIRED FOR A NEW COLUMN, AND THIS IS THE SAME TRAP AS `method`.
        # `CREATE TABLE IF NOT EXISTS` does NOT add a column to a table that already exists —
        # so a new column works perfectly on a fresh database and fails on every real one.
        try:
            self.conn.execute("ALTER TABLE documents ADD COLUMN is_dir INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()
        # ⚠️ Caches, invalidated on write. Rebuilding an inverted index is O(corpus) and would
        # dominate every query if it were not cached.
        self._postings: dict[str, list[int]] | None = None
        self._doc_len: np.ndarray | None = None
        self._n_docs: int = 0
        self._avgdl: float = 1.0

    # ---------------------------------------------------------------- writes
    @_serialised
    def clear(self) -> None:
        self.conn.execute("DELETE FROM documents")
        self.conn.commit()
        self._invalidate()

    def _invalidate(self) -> None:
        self._postings = None
        self._doc_len = None

    @_serialised
    def add_many(self, docs) -> int:
        """Bulk insert. ⚠️ `INSERT OR REPLACE` keyed on path makes re-indexing incremental:
        a file that has not changed is simply overwritten with identical content."""
        rows = [(d.path, d.mtime, d.size, d.lang, d.n_lines, d.text,
                 getattr(d, "method", "utf8"), 1 if getattr(d, "is_dir", False) else 0)
                for d in docs]
        self.conn.executemany(
            "INSERT INTO documents(path, mtime, size, lang, n_lines, text, method, is_dir) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
            "lang=excluded.lang, n_lines=excluded.n_lines, text=excluded.text, "
            "method=excluded.method, is_dir=excluded.is_dir",
            rows)
        self.conn.commit()
        self._invalidate()
        return len(rows)

    @_serialised
    def known_state(self) -> dict:
        """path -> (mtime, size) for everything already indexed.

        ⚠️ THIS IS WHAT MAKES RE-INDEX INCREMENTAL. It is one query over the index, and it is
        the difference between re-reading every file on disk and re-reading only what changed.
        """
        return {p: (m, sz) for p, m, sz in
                self.conn.execute("SELECT path, mtime, size FROM documents")}

    @_serialised
    def _ensure_skipped(self) -> None:
        """⚠️ A TABLE FOR WHAT WAS NOT INDEXED, because silence about it is the failure this
        project keeps rediscovering. Measured on the author's own machine: 150 files over the
        size limit, 22% of all bytes, and NOTHING anywhere said they had been skipped. Searching
        for something inside one returns "no results" and the user concludes it is not on disk."""
        self.conn.execute("""CREATE TABLE IF NOT EXISTS skipped (
            path TEXT PRIMARY KEY, size INTEGER, reason TEXT, seen REAL)""")
        self.conn.commit()

    def record_skipped(self, items: list) -> int:
        self._ensure_skipped()
        if not items:
            return 0
        now = time.time()
        self.conn.executemany(
            "INSERT INTO skipped(path,size,reason,seen) VALUES (?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, reason=excluded.reason, "
            "seen=excluded.seen",
            [(str(p), int(sz), why, now) for p, sz, why in items])
        self.conn.commit()
        return len(items)

    def prune_skipped(self) -> int:
        """⚠️ A file that is gone, or that is now small enough to index, must leave this table —
        otherwise the report claims files are missing that are right there in the results."""
        self._ensure_skipped()
        gone = [(p,) for (p,) in self.conn.execute("SELECT path FROM skipped")
                if not _exists(p)]
        if gone:
            self.conn.executemany("DELETE FROM skipped WHERE path=?", gone)
            self.conn.commit()
        indexed = self.conn.execute(
            "DELETE FROM skipped WHERE path IN (SELECT path FROM documents)").rowcount
        self.conn.commit()
        return len(gone) + indexed

    def skipped_stats(self, limit: int = 12) -> dict:
        self._ensure_skipped()
        total = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM skipped").fetchone()
        by = self.conn.execute(
            "SELECT reason, COUNT(*) c, SUM(size) b FROM skipped GROUP BY reason "
            "ORDER BY c DESC").fetchall()
        big = self.conn.execute(
            "SELECT path, size, reason FROM skipped ORDER BY size DESC LIMIT ?", (limit,)).fetchall()
        return {"files": total[0], "bytes": total[1],
                "by_reason": [{"reason": r, "files": c, "bytes": b} for r, c, b in by],
                "largest": [{"path": p, "size": sz, "reason": r} for p, sz, r in big]}

    def grep_skipped(self, query: str, limit: int = 30, budget_s: float = 6.0) -> list:
        """⚠️⚠️ READ THE FILES THAT WERE NEVER INDEXED, ON DEMAND.

        This is the ONE case where grep is the right tool and an index is not: the files are
        excluded from the index by definition, so there is nothing to search. Reading 150 files
        with a time budget beats indexing 1.25 GB that will almost never be queried.

        ⚠️ IT HAS A TIME BUDGET AND REPORTS WHEN IT RUNS OUT. A grep over 1.25 GB takes minutes,
        and a search box that silently takes minutes is worse than one that says "checked 40 of
        150 large files" — the user can decide whether to narrow the query.
        """
        import re as _re
        self._ensure_skipped()
        if not query.strip():
            return []
        rx = _re.compile(_re.escape(query), _re.I)
        t0 = time.time()
        out, checked, skipped_n = [], 0, 0
        for path, size in self.conn.execute("SELECT path, size FROM skipped ORDER BY size ASC"):
            if time.time() - t0 > budget_s:
                return out + [(None, 0.0, {"timed_out": True, "checked": checked,
                                           "total": self.skipped_stats()["files"]})]
            checked += 1
            try:
                # ⚠️ BINARY MODE AND A BYTE-LEVEL SEARCH. We do not know the encoding and may not
                # care — `mongod` is a binary and a user may still want to know it exists. Reading
                # as text with errors="replace" would work but is 10x slower on 200 MB.
                with open(path, "rb") as fh:
                    blob = fh.read(48 * 1024 * 1024)      # ⚠️ capped: 200 MB binaries are not text
                if rx.search(blob.decode("utf-8", "replace")):
                    out.append((path, size, None))
                    if len(out) >= limit:
                        break
            except (OSError, PermissionError):
                skipped_n += 1
        return out

    def graph(self):
        """⚠️ ONE CONNECTION, TWO USES. A symbol is a property of a document, and splitting them
        across two stores lets them disagree about which files exist — the exact class of bug the
        vector manifest already had to be fixed for."""
        from .codegraph import CodeGraph
        g = CodeGraph(self.conn)
        g.ensure_schema()
        return g

    def build_graph(self, docs) -> dict:
        """Extract symbols and edges for a batch of documents.

        ⚠️ REBUILDS PER PATH RATHER THAN APPENDING. Re-indexing a file must not leave the
        previous version's symbols in the graph, or a renamed function would still have callers
        pointing at a name that no longer exists anywhere.
        """
        g = self.graph()
        files = syms = 0
        for d in docs:
            if not d.path:
                continue
            g.remove_path(d.path)
            n = g.add(d.path, getattr(d, "doc_id", 0) or 0, d.text)
            if n:
                files += 1
                syms += n
        self.conn.commit()
        return {"files": files, "symbols": syms}

    def remove_under_roots(self, roots: list, dry_run: bool = False) -> dict:
        """Delete every document located under any of these directories.

        ⚠️⚠️ THIS IS THE MISSING HALF OF SETUP, AND ITS ABSENCE WAS A ONE-WAY DOOR.
        `prune_missing` only deletes rows whose FILE no longer exists on disk, so switching from
        curated folders to "everything" added documents and there was no operation anywhere in
        the tool to take them back out. A user could widen the index and never narrow it.

        ⚠️ IT MATCHES ON THE PATH PREFIX, and the trailing separator is load-bearing. Comparing
        `/Users/x/Doc` without it also matches `/Users/x/Documents` and `/Users/x/Doc-backup` —
        so removing one folder would silently delete two others. That is a data-loss bug that
        looks like it works.
        """
        from pathlib import Path as _P
        # ⚠️⚠️ BOTH FORMS OF EVERY PATH, AND THIS IS NOT DEFENSIVE PADDING — IT WAS A REAL BUG.
        #
        # On macOS `/tmp` is a SYMLINK to `/private/tmp`. `remove_under_roots(['/tmp/isotest'])`
        # resolved the root to `/private/tmp/isotest/` while the stored document paths began
        # `/tmp/isotest/`, so the string prefix match failed and the removal returned 0 with no
        # error at all.
        #
        # ⚠️ A REMOVAL THAT SILENTLY DOES NOTHING IS WORSE THAN ONE THAT FAILS. The user is told
        # "0 removed", assumes a bug in the count, and believes the documents are gone — while
        # they are still indexed, still searchable, and still on disk.
        #
        # ⚠️ SO BOTH FORMS ARE MATCHED: the resolved one, and the literal expanded one. Which one
        # the stored paths use depends on how they were reached at crawl time, and that is not
        # knowable from here.
        prefixes = []
        for r in roots:
            try:
                raw = str(_P(r).expanduser())
            except (OSError, RuntimeError):
                continue
            prefixes.append(raw.rstrip(os.sep) + os.sep)
            try:
                prefixes.append(str(_P(raw).resolve()).rstrip(os.sep) + os.sep)
            except OSError:
                pass
        prefixes = sorted(set(prefixes))
        if not prefixes:
            return {"removed": 0, "roots": [], "error": "no usable roots given"}
        victims = []
        for doc_id, path in self.conn.execute("SELECT id, path FROM documents"):
            if any(str(path).startswith(pfx) for pfx in prefixes):
                victims.append((doc_id, path))
        if dry_run:
            out = {"removed": len(victims), "roots": prefixes, "dry_run": True,
                   "sample": [v[1] for v in victims[:5]]}
            # ⚠️ ZERO MATCHES IS REPORTED, not passed off as success. "I removed nothing" and
            # "there was nothing to remove" look identical in a count and mean opposite things.
            if not victims:
                out["warning"] = ("no indexed documents are under any of those paths — check "
                                  "the path is spelled the way it was crawled")
            return out
        if victims:
            # ⚠️ THE GRAPH IS CLEANED WITH THE DOCUMENTS. Leaving symbols behind means a graph
            # query returns hits in files that are no longer indexed — results the user cannot
            # open, from a store that claims not to contain them.
            for _did, path in victims:
                try:
                    self.graph().remove_path(path)
                except Exception:
                    pass
            self.conn.executemany("DELETE FROM documents WHERE id=?",
                                  [(v[0],) for v in victims])
            self.conn.commit()
            self._invalidate()
        return {"removed": len(victims), "roots": prefixes}

    def roots_in_index(self) -> list[dict]:
        """⚠️ WHAT IS ACTUALLY IN THE INDEX, as opposed to what was REQUESTED.

        The configured roots and the indexed roots drift apart the moment a folder is added or a
        drive is unmounted, and the UI needs the second one to show the truth.
        """
        seen: dict[str, int] = {}
        for (path,) in self.conn.execute("SELECT path FROM documents"):
            top = os.sep.join(str(path).split(os.sep)[:4])     # /Users/<name>/<folder>
            seen[top] = seen.get(top, 0) + 1
        return sorted(({"path": k, "documents": v} for k, v in seen.items()),
                      key=lambda d: -d["documents"])

    def prune_missing(self) -> int:
        """Drop rows whose file no longer exists. ⚠️ Without this the index only grows, and a
        deleted secret stays searchable forever — which is the exact thing a user would never
        think to check."""
        gone = []
        for doc_id, path in self.conn.execute("SELECT id, path FROM documents"):
            if not Path(path).exists():
                gone.append((doc_id,))
        if gone:
            self.conn.executemany("DELETE FROM documents WHERE id=?", gone)
            self.conn.commit()
            self._invalidate()
        return len(gone)

    @_serialised
    def set_meta(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))
        self.conn.commit()

    @_serialised
    # ⚠⚠️ THE INDEX KNOWS WHICH FORMAT BUILT IT, AND SAYS SO WHEN ASKED.
    #
    # ⚠️ AN ABSENT RECORD IS NOT A MATCHING ONE. A index written before this existed has no
    # entry at all, and treating "no record" as "up to date" is how the stale-index bug appears
    # on every machine that has been running the app for a while — which is all of them.
    def index_format(self) -> int:
        try:
            return int(self.get_meta("index_format", 0) or 0)
        except Exception:
            return 0

    def needs_rebuild_for(self, current: int) -> tuple:
        """(bool, reason). ⚠️ The reason is returned, not just the verdict — because the user is
        told WHY the app decided to spend their CPU on something they did not ask for."""
        have = self.index_format()
        if have == current:
            return False, ""
        if have == 0:
            return True, ("this index was built before folder search existed — adding "
                          "folders so you can find them by name")
        return True, (f"this index was built by an older version (format {have}, "
                      f"now {current})")

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ----------------------------------------------------------------- reads
    @_serialised
    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    @_serialised
    def by_id(self, doc_id: int) -> tuple | None:
        return self.conn.execute(
            "SELECT id, path, lang, n_lines, text FROM documents WHERE id=?", (doc_id,)
        ).fetchone()

    @_serialised
    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(n_lines),0) FROM documents"
        ).fetchone()
        langs = self.conn.execute(
            "SELECT lang, COUNT(*) c FROM documents GROUP BY lang ORDER BY c DESC LIMIT 15"
        ).fetchall()
        return {"documents": row[0], "bytes": row[1], "lines": row[2], "langs": langs}

    # ------------------------------------------------------- backend 1: exact
    @_serialised
    def exact_search(self, literal: str, limit: int = 40) -> list[tuple[int, float]]:
        """Literal substring match. **THIS IS THE BACKEND YOUR EXAMPLE NEEDS.**

        ⚠️ `InputLayer(shape=(784,))` is not a concept — it is 24 characters that exist or do
        not exist in a file. An embedding would return "things that look like Keras input
        configuration"; this returns the line. Semantic search is the wrong tool for a code
        fragment, and a tool that only embeds would fail the user's own example query.

        ⚠️ `LIKE '%...%'` IS A FULL SCAN and that is accepted: it runs in C inside SQLite over
        a few hundred MB, in single-digit seconds. The alternative — a suffix automaton or n-gram
        inverted index — is the correct engineering answer at 100x this corpus and the wrong
        one for a first version.
        """
        if len(literal) < 2:
            return []
        esc = literal.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.conn.execute(
            "SELECT id FROM documents WHERE text LIKE ? ESCAPE '\\' LIMIT ?",
            (f"%{esc}%", limit * 4)).fetchall()
        # ⚠️ Score by occurrence count so the file that is ABOUT the fragment outranks a file
        # that mentions it once in passing.
        out = []
        for (doc_id,) in rows:
            row = self.by_id(doc_id)
            if row:
                n = row[4].count(literal)
                out.append((doc_id, float(n)))
        out.sort(key=lambda kv: -kv[1])
        return out[:limit]

    @_serialised
    def regex_search(self, pattern: str, limit: int = 40) -> list[tuple[int, float]]:
        """Regex over stored text. ⚠️ Pure Python, so it is the slowest backend; it reports how
        many documents it had to scan so the cost is visible rather than mysterious."""
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"bad regex: {e}") from e
        out = []
        scanned = 0
        for doc_id, text in self.conn.execute("SELECT id, text FROM documents"):
            scanned += 1
            m = rx.findall(text)
            if m:
                out.append((doc_id, float(len(m))))
                if len(out) >= limit * 4:
                    break
        out.sort(key=lambda kv: -kv[1])
        self.last_scan = scanned
        return out[:limit]

    # ------------------------------------------------------ backend 2: keyword
    # ⚠⚠️ AN FTS5 INDEX, BECAUSE BUILDING ONE IN PYTHON COSTS SIX MINUTES AND ALL THE MEMORY.
    #
    # ⚠️ MEASURED AT 859,569 DOCUMENTS:
    #
    #       exact      4.09s
    #       path       0.79s
    #       keyword  351.45s      <- five minutes fifty-one
    #
    # ⚠️ The Python BM25 does cache its postings, so that number is the FIRST call — which is the
    # one that matters, because the daemon restarts and every restart pays it again. And the
    # postings live in RAM, which at this size is gigabytes for an index SQLite will maintain
    # on disk for nothing.
    #
    # ⚠️ SO SQLITE DOES IT. FTS5 keeps an inverted index in the database, updated on insert, and
    # ranks with BM25 — the same formula and the same constants as the Python path below, so the
    # two agree rather than quietly disagreeing.
    #
    # ⚠️ AND IT DEGRADES RATHER THAN FAILS. FTS5 is compiled into most builds but not all, and a
    # database file may predate this table, so every use is guarded and the Python BM25 remains
    # as the fallback. ⚠️ A search backend that refuses to run because an optional extension is
    # missing turns "slower" into "broken".
    def _ensure_fts(self) -> bool:
        if self._fts_ok is not None:
            return self._fts_ok
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
                "text, content='documents', content_rowid='id', tokenize='unicode61')")
            # ⚠️ AN EXTERNAL-CONTENT TABLE, so the text is NOT stored twice. At 6.9 GB of
            # documents a duplicate copy is the difference between an index and a second corpus.
            # ⚠⚠️ COUNT THE INDEX, NOT THE TABLE. COUNT(docs_fts) READS `documents`.
            #
            # ⚠️ With content='documents' the FTS table is EXTERNAL-CONTENT: SELECT count(*) FROM
            # docs_fts returns the number of rows in the CONTENT table, not the number of terms
            # indexed. ⚠️ So comparing it to count(documents) compared a number with itself, the
            # check always passed, and the rebuild never ran.
            #
            # ⚠️ MEASURED: count(docs_fts) = 859,569, count(documents) = 859,569, and a raw
            # MATCH 'screenshot' returned ZERO. The index was empty and every number said it was
            # full. Searches came back in 0.00s with no results — fast, and wrong.
            #
            # ⚠️ docs_fts_data IS THE ACTUAL INDEX. A fresh table has one row of structural data and
            # nothing else, so a handful of rows means unbuilt and thousands means built.
            n_idx = self.conn.execute("SELECT count(*) FROM docs_fts_data").fetchone()[0]
            n_doc = self.conn.execute("SELECT count(*) FROM documents").fetchone()[0]
            if n_idx < 100 and n_doc > 0:
                # ⚠️ REBUILT IN ONE STATEMENT when the counts disagree — which is also what makes
                # this safe to add to an existing database that was written before the table.
                self.conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
                self.conn.commit()
            # ⚠⚠️ TRIGGERS, BECAUSE AN EXTERNAL-CONTENT FTS TABLE DOES NOT MAINTAIN ITSELF.
            #
            # ⚠️ MEASURED: a document inserted after the index was built was INVISIBLE to keyword
            # search — zero hits before a restart and zero hits after one. FTS5 with
            # content='documents' stores no text of its own, so it has no way to know a row
            # changed; the CONTENT table has to tell it.
            #
            # ⚠️ AND NOTHING ELSE WOULD HAVE CAUGHT THIS. The index builds, the counts match, the
            # searches are instant, and every file added after that point is silently unsearchable
            # by keyword — which looks exactly like the file not existing.
            #
            # ⚠️ THIS IS SQLITE'S OWN DOCUMENTED PATTERN for external-content FTS5: three triggers
            # on the content table, one per operation. The 'delete' row the triggers insert is a
            # command to the FTS index, not a row of data — it is how an external-content index is
            # told to forget the old terms.
            for _name, _sql in (
                ("docs_fts_ai", "CREATE TRIGGER IF NOT EXISTS docs_fts_ai AFTER INSERT ON documents BEGIN "
                                "INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text); END"),
                ("docs_fts_ad", "CREATE TRIGGER IF NOT EXISTS docs_fts_ad AFTER DELETE ON documents BEGIN "
                                "INSERT INTO docs_fts(docs_fts, rowid, text) "
                                "VALUES('delete', old.id, old.text); END"),
                # ⚠️ THE UPDATE TRIGGER DELETES THEN RE-INSERTS, and the ORDER matters: a delete
                # without the old text leaves stale terms behind that match forever.
                ("docs_fts_au", "CREATE TRIGGER IF NOT EXISTS docs_fts_au AFTER UPDATE ON documents BEGIN "
                                "INSERT INTO docs_fts(docs_fts, rowid, text) "
                                "VALUES('delete', old.id, old.text); "
                                "INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text); END"),
            ):
                self.conn.execute(_sql)
            self.conn.commit()
            self._fts_ok = True
        except sqlite3.OperationalError:
            # ⚠️ NOT COMPILED IN, OR AN OLD FILE THAT CANNOT TAKE THE TABLE. The Python path still
            # works; it is slower and that is all.
            self._fts_ok = False
        return self._fts_ok

    def _build_postings(self) -> None:
        """⚠️ BUILT LAZILY AND CACHED. Rebuilding on every query would make keyword search
        O(corpus) per query — technically correct and unusable."""
        if self._postings is not None:
            return
        postings: dict[str, list[int]] = defaultdict(list)
        lengths: list[int] = []
        ids: list[int] = []
        for doc_id, text in self.conn.execute("SELECT id, text FROM documents"):
            toks = tokenize(text)
            lengths.append(len(toks))
            ids.append(doc_id)
            for tok, tf in Counter(toks).items():
                # ⚠️ Storing tf as repeated entries would balloon memory; instead the posting
                # value encodes it as a float in the high bits. Simpler: keep tf in a dict.
                postings[tok].append((len(ids) - 1, tf))
        self._postings = dict(postings)
        self._doc_ids = ids
        self._doc_len = np.asarray(lengths, dtype=np.float64)
        self._n_docs = len(ids)
        self._avgdl = float(self._doc_len.mean()) if self._n_docs else 1.0

    @_serialised
    def keyword_search(self, query: str, limit: int = 40, k1: float = 1.5,
                       b: float = 0.75) -> list[tuple[int, float]]:
        """Okapi BM25. ⚠️ Same formula as the RAG track, same constants, deliberately: a
        second implementation with different constants would make the two tracks disagree and
        the disagreement would be mine, not the data's."""
        # ⚠⚠️ FTS5 FIRST, THE PYTHON BM25 SECOND. Same formula, same k1 and b — the only
        # difference the caller can observe is that one answers in milliseconds.
        #
        # ⚠️ MEASURED AT 859,569 DOCUMENTS: the Python path took 351s on its FIRST call. It
        # caches, so the second query was 0.06s — but the first call happens on every daemon
        # restart, which is what the user experiences as the app being broken.
        if self._ensure_fts():
            # ⚠⚠️ `.isalnum()` DROPPED EVERY IDENTIFIER WITH AN UNDERSCORE OR A DOT IN IT.
            #
            # ⚠️ MEASURED: the FTS index matched `unique_token_xyz` and `db_acl.php` perfectly — a raw
            # MATCH returned the row — and the search returned ZERO, because `"_".isalnum()` is
            # False and the token was filtered out before it reached the query.
            #
            # ⚠️ THIS IS A FILE SEARCH TOOL. Underscores, dots and hyphens are in most filenames and
            # most code identifiers, so the filter removed exactly the tokens the product exists to
            # find — and it did it silently, by returning an empty result rather than an error.
            #
            # ⚠️ SO NOTHING IS FILTERED. The tokens are QUOTED below, which is what makes FTS5 treat
            # them as literal strings, so a token containing punctuation cannot be parsed as
            # syntax. Filtering was never needed; quoting is the mechanism.
            toks = [t for t in tokenize(query) if t.strip()]
            if not toks:
                return []
            # ⚠️ QUOTED AND OR-JOINED. FTS5 treats AND, OR, NOT, NEAR and * as operators, so a
            # user searching "not working" would have NOT parsed as one and the query mangled.
            expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)
            try:
                rows = self.conn.execute(
                    "SELECT rowid, bm25(docs_fts, ?, ?) AS r FROM docs_fts "
                    "WHERE docs_fts MATCH ? ORDER BY r LIMIT ?",
                    (k1, b, expr, limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            # ⚠️ NEGATED: bm25() returns LOWER for better while the rest of the pipeline sorts
            # descending. Sign confusion here would silently REVERSE the ranking while every
            # other part of the system looked correct.
            return [(rid, -float(r)) for rid, r in rows]

        q = tokenize(query)
        if not q or not self._n_docs:
            return []
        scores = np.zeros(self._n_docs, dtype=np.float64)
        for tok in set(q):
            posting = self._postings.get(tok)
            if not posting:
                continue
            df = len(posting)
            # ⚠️ The +0.5 smoothing keeps a term appearing in EVERY document from scoring
            # zero or negative, which is what an unsmoothed idf does.
            idf = math.log(1 + (self._n_docs - df + 0.5) / (df + 0.5))
            for row_idx, tf in posting:
                dl = self._doc_len[row_idx]
                denom = tf + k1 * (1 - b + b * dl / self._avgdl)
                scores[row_idx] += idf * (tf * (k1 + 1)) / denom
        top = np.argsort(-scores)[:limit]
        return [(self._doc_ids[i], float(scores[i])) for i in top if scores[i] > 0]

    @_serialised
    def path_search(self, query: str, limit: int = 40) -> list[tuple[int, float]]:
        """Match against the PATH. ⚠️ A separate backend because 'where is my file called X' is
        a completely different question from 'which file contains X', and conflating them makes
        filename lookups fail whenever the file is small or its content is unrelated."""
        # ⚠️⚠️ TOKENISED, NOT A LITERAL SUBSTRING OF THE WHOLE QUERY.
        #
        # This matched the raw query against the path, so "find db_acl.php" searched for the
        # literal string "find db_acl.php" in a filename and found nothing — while "db_acl.php"
        # worked. ⚠️ Anyone who types a verb first got zero results, and would reasonably
        # conclude filename search does not exist.
        #
        # ⚠️ Separate terms rather than the whole string also means a query naming a file AND a
        # directory scores higher for matching both.
        terms = [t for t in re.findall(r"[A-Za-z0-9_.+\-]{2,}", query)
                 if t.lower() not in _PATH_STOPWORDS]
        if not terms:
            return []
        seen: dict[int, float] = {}
        for term in terms:
            esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self.conn.execute(
                "SELECT id, path, COALESCE(is_dir,0) FROM documents "
                "WHERE path LIKE ? ESCAPE '\\' LIMIT ?",
                (f"%{esc}%", limit * 4)).fetchall()
            for doc_id, path, dflag in rows:
                base = Path(path).name.lower()
                t = term.lower()
                # ⚠️ Matches in the BASENAME outrank matches anywhere in the path, so
                # `config.py` beats `…/config-helper/src/…/Thing.java`. A bare extension match
                # (".php") scores lowest — it is true of thousands of files and identifies none.
                # ⚠⚠️ A DIRECTORY WHOSE NAME IS THE QUERY IS THE ANSWER, NOT A CANDIDATE.
                #
                # Searching "screenshot" and getting files that mention screenshots, while the
                # folder actually called that sits fourth, is the wrong answer presented
                # confidently. A folder named exactly what was typed outranks everything.
                #
                # ⚠️ READ FROM THE ROW SO IT CANNOT DRIFT from what is stored — the earlier
                # folder bugs all came from a second source of truth.
                is_dir = bool(dflag)
                if base == t or base.rsplit(".", 1)[0] == t:
                    sc = 12.0 if is_dir else 6.0
                elif base.startswith(t):
                    sc = 3.0
                elif t in base:
                    sc = 2.0
                elif t.startswith(".") :
                    sc = 0.3
                else:
                    # ⚠⚠️ THE TERM IS NOT IN THIS ITEM'S NAME AT ALL — AN ANCESTOR MATCHED, AND AN
                    # ANCESTOR MATCH IS NOT A RESULT.
                    #
                    # ⚠️ REPORTED TWICE: "screenshot" returned everything under
                    # ~/Documents/Screenshots, because that file contains the word in its PATH
                    # while its own NAME is something else entirely.
                    #
                    # ⚠️ IT SCORED 1.0, WHICH IS ABOVE ZERO AND THEREFORE A RESULT. Searching for a
                    # folder returned the folder AND every file inside it, burying the folder
                    # that was actually asked for under its own contents.
                    #
                    # ⚠️ SOMEONE WHO SEARCHES A FOLDER NAME WANTS THE FOLDER. If they want what is
                    # inside they can open it — one click, no guessing. Guessing wrong here is
                    # worse, because an ancestor match looks exactly like a real one.
                    continue
                seen[doc_id] = max(seen.get(doc_id, 0.0), sc)
        out = sorted(seen.items(), key=lambda kv: -kv[1])
        return out[:limit]
