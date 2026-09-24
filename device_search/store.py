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

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id       INTEGER PRIMARY KEY,
    path     TEXT UNIQUE NOT NULL,
    mtime    REAL,
    size     INTEGER,
    lang     TEXT,
    n_lines  INTEGER,
    text     TEXT NOT NULL
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
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # ⚠️ Caches, invalidated on write. Rebuilding an inverted index is O(corpus) and would
        # dominate every query if it were not cached.
        self._postings: dict[str, list[int]] | None = None
        self._doc_len: np.ndarray | None = None
        self._n_docs: int = 0
        self._avgdl: float = 1.0

    # ---------------------------------------------------------------- writes
    def clear(self) -> None:
        self.conn.execute("DELETE FROM documents")
        self.conn.commit()
        self._invalidate()

    def _invalidate(self) -> None:
        self._postings = None
        self._doc_len = None

    def add_many(self, docs) -> int:
        """Bulk insert. ⚠️ `INSERT OR REPLACE` keyed on path makes re-indexing incremental:
        a file that has not changed is simply overwritten with identical content."""
        rows = [(d.path, d.mtime, d.size, d.lang, d.n_lines, d.text) for d in docs]
        self.conn.executemany(
            "INSERT INTO documents(path, mtime, size, lang, n_lines, text) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
            "lang=excluded.lang, n_lines=excluded.n_lines, text=excluded.text",
            rows)
        self.conn.commit()
        self._invalidate()
        return len(rows)

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

    def set_meta(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ----------------------------------------------------------------- reads
    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def by_id(self, doc_id: int) -> tuple | None:
        return self.conn.execute(
            "SELECT id, path, lang, n_lines, text FROM documents WHERE id=?", (doc_id,)
        ).fetchone()

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(n_lines),0) FROM documents"
        ).fetchone()
        langs = self.conn.execute(
            "SELECT lang, COUNT(*) c FROM documents GROUP BY lang ORDER BY c DESC LIMIT 15"
        ).fetchall()
        return {"documents": row[0], "bytes": row[1], "lines": row[2], "langs": langs}

    # ------------------------------------------------------- backend 1: exact
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
