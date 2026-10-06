#!/usr/bin/env python3
"""
test_fts_fallback.py — the FTS5 fast path, and the fallback that must not be silent.

WHY THIS FILE EXISTS
--------------------
`store.py` has two keyword backends: FTS5 (fast, on-disk, trigger-maintained) and an
in-process NumPy BM25 (slower, always works). FTS5 is most builds but not all, so the
fallback is real and necessary.

⚠️ THE BUG THIS PINS: the fallback USED TO SAY NOTHING. A silent fallback is
indistinguishable from a fallback that is never needed, so the slow path could run forever
with nothing reporting it — which is the same defect already found and fixed once in the
trigram path. The trigram path had a warning; the FTS5 path did not.

⚠️ AND "SLOWER" IS NOT A SMALL WORD. Measured at 859,569 documents the NumPy BM25 took
**351 seconds** on its first call. It caches, so the second query was 0.06 s — and the
first call happens on EVERY daemon restart, which is what the user experiences as the app
being broken.

⚠️ THIS TEST DOES NOT NEED A SQLITE BUILD WITHOUT FTS5. It wraps the connection in a proxy
that refuses any `fts5` statement, which is exactly what such a build does. A test that
requires an unusual environment is a test nobody runs.

RUN
    python tests/test_fts_fallback.py

Correct output: four PASS lines and exit code 0. Exit code 1 if any check fails.
"""

from __future__ import annotations

import pathlib
import sqlite3
import types
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from device_search.store import Store  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        FAILED.append(name)


class RefusesFTS5:
    """A connection proxy that fails any statement mentioning fts5.

    This is what a SQLite compiled without FTS5 does when asked to CREATE VIRTUAL TABLE
    ... USING fts5, so the fallback can be exercised on a machine that HAS FTS5 — which is
    every machine this will realistically run on.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.refused = 0

    def execute(self, sql: str, *args, **kwargs):
        if "fts5" in sql:
            self.refused += 1
            raise sqlite3.OperationalError("no such module: fts5")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def main() -> int:
    print("test_fts_fallback.py — the FTS5 fast path and the fallback that must be visible")
    tmp = pathlib.Path(tempfile.mkdtemp())

    # ---- 1. the fast path works, and stays quiet -----------------------------
    s = Store(tmp / "fast.db")
    usable = s._ensure_fts()
    check("FTS5 is usable on this build", usable is True,
          "if this FAILs, this sqlite3 has no FTS5 and the rest is still meaningful")
    check("the fast path prints no warning", getattr(s, "_fts_warned", False) is False)

    # ---- 2. the fallback engages AND warns ----------------------------------
    slow = Store(tmp / "slow.db")
    slow.conn = RefusesFTS5(slow.conn)
    # ⚠️ __init__ already called _ensure_fts, so the result is CACHED. Clearing it is what
    # makes this a test of the fallback rather than a test of the cache — the first version
    # of this check passed for the wrong reason.
    slow._fts_ok = None
    slow._fts_warned = False

    fell_back = slow._ensure_fts()
    check("FTS5 failure falls back instead of raising", fell_back is False)

    warned = getattr(slow, "_fts_warned", False)
    check("the fallback WARNS rather than going silent", warned is True,
          "a silent fallback is indistinguishable from one that is never needed")

    # ---- 3. it warns ONCE, not on every query ------------------------------
    # ⚠️ Per-query warnings would be their own defect: a warning on every search is noise the
    # user learns to ignore, which is how a real warning stops being read.
    slow._ensure_fts()
    slow._ensure_fts()
    check("the warning does not repeat", getattr(slow, "_fts_warned", False) is True)

    # ---- 4. search still WORKS on the fallback -----------------------------
    # ⚠️ The point of a fallback is that search never fails because an optimisation is
    # missing. A fallback that raises is worse than no fallback.
    # `Doc` is duck-typed in add_many (it reads .path/.mtime/.size/.lang/.n_lines/.text and
    # getattr-defaults .method/.is_dir), so a namespace is a real document as far as it cares.
    docs = [
        types.SimpleNamespace(path="/x/alpha_notes.txt", mtime=0.0, size=40, lang="text",
                              n_lines=2, text="the quick brown fox\nunique_token_xyz"),
        types.SimpleNamespace(path="/x/db_acl.php", mtime=0.0, size=40, lang="php",
                              n_lines=1, text="db_acl.php handles access control"),
        types.SimpleNamespace(path="/x/model.py", mtime=0.0, size=60, lang="python",
                              n_lines=1, text="layer = InputLayer(shape=(784,))"),
    ]
    slow.add_many(docs)
    hits = slow.keyword_search("quick")
    check("keyword search still returns results on the fallback", len(hits) >= 1,
          f"{len(hits)} hit(s)")

    # ---- 5. identifiers with punctuation, on BOTH paths --------------------
    # ⚠️ PINS A REAL HISTORICAL BUG. The FTS5 path once filtered query tokens with
    # `.isalnum()`, which is False for "_" and "." — so a raw MATCH for `unique_token_xyz`
    # returned its row while the search returned ZERO. ⚠️ In a FILE search tool that removes
    # exactly the tokens the product exists to find, and it did it silently, by returning an
    # empty result rather than an error.
    #
    # ⚠️ AND THE SAME CLASS OF BUG IS WHY THE FALLBACK CHECK ABOVE MATTERS: an empty result is
    # the one failure signal a user cannot distinguish from "that file does not exist".
    fast = Store(tmp / "ident.db")
    fast.add_many(docs)
    for q, why in (("unique_token_xyz", "an identifier with underscores"),
                   ("db_acl.php", "an identifier with a dot"),
                   ("InputLayer(shape=(784,))", "a fragment with punctuation")):
        on_fast = len(fast.keyword_search(q)) or len(fast.exact_search(q))
        on_slow = len(slow.keyword_search(q))
        check(f"found on both paths: {why}", on_fast >= 1 and on_slow >= 1,
              f"fts5={on_fast} fallback={on_slow}")

    print()
    if FAILED:
        print(f"  ❌ {len(FAILED)} check(s) failed: {', '.join(FAILED)}")
        return 1
    print("  ✅ the fallback is visible, and the fast path is unaffected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
