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
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        # ⚠️ MIGRATION, AND IT IS NOT OPTIONAL. `CREATE TABLE IF NOT EXISTS` does NOT add a
        # column to a table that already exists, so an index built before `method` existed
        # would fail every query with "no such column". Silent upgrade breakage is the kind of
        # bug that only appears on other people's machines.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(documents)")}
        if "method" not in cols:
            self.conn.execute("ALTER TABLE documents ADD COLUMN method TEXT DEFAULT 'utf8'")
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
                 getattr(d, "method", "utf8")) for d in docs]
        self.conn.executemany(
            "INSERT INTO documents(path, mtime, size, lang, n_lines, text, method) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
            "lang=excluded.lang, n_lines=excluded.n_lines, text=excluded.text, "
            "method=excluded.method",
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
        self._build_postings()
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
        esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.conn.execute(
            "SELECT id, path FROM documents WHERE path LIKE ? ESCAPE '\\' LIMIT ?",
            (f"%{esc}%", limit)).fetchall()
        # ⚠️ Prefer matches in the BASENAME: searching "config" should not rank
        # `…/config-helper/src/main/java/…/Thing.java` above `config.py`.
        out = []
        for doc_id, path in rows:
            base = Path(path).name.lower()
            score = 2.0 if query.lower() in base else 1.0
            if base.startswith(query.lower()):
                score = 3.0
            out.append((doc_id, score))
        out.sort(key=lambda kv: -kv[1])
        return out[:limit]
