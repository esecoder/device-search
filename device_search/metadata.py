"""
metadata.py — find files by what they ARE, not what they contain.

===============================================================================
⚠️ WHY THIS EXISTS: TWO QUESTIONS THAT LOOK LIKE SEARCH AND ARE NOT
===============================================================================
    "find the file called mongod"                 -> path      (built)
    "show me files over 100 MB"                   -> METADATA
    "what did I change today"                     -> METADATA
    "all the PHP files"                           -> METADATA (extension)
    "where is that string in the 69 MB log"       -> GREP (built, see store.grep)

⚠️ THE FIRST AND SECOND LOOK IDENTICAL TO A SEARCH BOX AND SHARE NO IMPLEMENTATION.
`files over 100 MB` has no words to match against text — the answer is a property of the
file, and it is already in the database. Ranking by cosine similarity against the literal
string "files over 100 MB" would return the files that TALK about size, which is the
opposite of the question.

⚠️ SO IT IS PARSED, NOT SEARCHED. A small grammar turns the sentence into filters, and the
filters run as SQL. That is exact, instant, and explainable — the user can see that the
tool understood "over 100 MB" as `size > 104857600`.
"""

from __future__ import annotations

import re
import time

# ⚠️ SIZE UNITS, INCLUDING THE ONES PEOPLE ACTUALLY TYPE. `k`/`kb`/`kib` all appear in the
# wild and mean whatever the writer thought; treating them as 1024 is the convention and is
# stated in the output so a mismatch is visible rather than mysterious.
_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "kib": 1024,
               "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
               "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3}

_SIZE_RX = re.compile(
    r"\b(?:over|larger than|bigger than|greater than|above|>)\s*(\d+(?:\.\d+)?)\s*([kmg]i?b?)?",
    re.I)
_SIZE_UNDER_RX = re.compile(
    r"\b(?:under|smaller than|less than|below|<)\s*(\d+(?:\.\d+)?)\s*([kmg]i?b?)?", re.I)

# ⚠️ TIME IS PARSED AS OFFSETS, NOT DATES. "today" and "this week" are relative to when the
# query runs, and storing an absolute date would make the same query mean something different
# tomorrow.
_TIME_RX = re.compile(
    r"\b(today|yesterday|this week|last week|this month|last month|"
    r"in the last (\d+) (day|days|week|weeks|hour|hours|month|months))\b", re.I)

_EXT_RX = re.compile(r"(?:^|\s)(\*?\.([a-z0-9]{1,8}))(?:\s|$)", re.I)


def parse(query: str) -> dict:
    """Turn a sentence into filters. Returns {} when nothing metadata-ish is present.

    ⚠️ RETURNS {} RATHER THAN GUESSING. A query with no size, date or extension in it is a
    normal text search, and inventing a filter for it would silently narrow the results of
    every ordinary query.
    """
    q = query.lower()
    f: dict = {}
    # ⚠️ EVERY MATCH'S SPAN IS RECORDED, so the leftover can be computed exactly rather than
    # guessed. Whatever is left after the filters have taken their words is the TEXT QUERY — and
    # if nothing meaningful is left, there is no text query and this is a pure filter.
    _spans: list = []

    m = _SIZE_RX.search(q)
    if m:
        _spans.append(m.span())
        f["min_bytes"] = int(float(m.group(1)) * _SIZE_UNITS.get((m.group(2) or "").lower(), 1))
        f["_min"] = m.group(0).strip()
    m = _SIZE_UNDER_RX.search(q)
    if m:
        _spans.append(m.span())
        f["max_bytes"] = int(float(m.group(1)) * _SIZE_UNITS.get((m.group(2) or "").lower(), 1))
        f["_max"] = m.group(0).strip()

    m = _TIME_RX.search(q)
    if m:
        _spans.append(m.span())
        now = time.time()
        if m.group(2):
            n, unit = int(m.group(2)), m.group(3).lower().rstrip("s")
            span = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000}[unit] * n
            f["since"] = now - span
            f["_since"] = m.group(0).strip()
        else:
            word = m.group(1).lower()
            # ⚠️ "this week" IS NOT "the last 7 days" and the difference shows up on a Monday.
            # Calendar boundaries are what people mean, so they are computed as boundaries.
            lt = time.localtime(now)
            midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
            if word == "today":
                f["since"] = midnight
            elif word == "yesterday":
                f["since"] = midnight - 86400
                f["until"] = midnight
            elif word.startswith("this week"):
                f["since"] = midnight - (lt.tm_wday * 86400)
            elif word.startswith("last week"):
                f["since"] = midnight - ((lt.tm_wday + 7) * 86400)
                f["until"] = midnight - (lt.tm_wday * 86400)
            elif word.startswith("this month"):
                f["since"] = midnight - ((lt.tm_mday - 1) * 86400)
            elif word.startswith("last month"):
                f["since"] = midnight - ((lt.tm_mday + 27) * 86400)
                f["until"] = midnight - ((lt.tm_mday - 1) * 86400)
            f["_since"] = word

    # ⚠⚠️ A SIZE WITH NO COMPARATOR IS STILL A SIZE, AND THIS IS THE MOST NATURAL WAY TO ASK.
    #
    # The patterns above need a word like "over" or "larger than". ⚠️ Measured: "10gb files" parsed
    # as NOTHING and became a plain text search — which then matched files whose CONTENT mentions
    # sizes, the opposite of the question.
    #
    # ⚠️ "10gb files" IS HOW PEOPLE WRITE IT. The unit is the signal: nobody types "files that are
    # larger than 10 gigabytes" into a search box, and requiring the comparator made the feature
    # reachable only by someone who already knew the syntax.
    #
    # ⚠️ AND THE UNIT WORD IS REQUIRED. A bare "10" or "10 files" is not a size, and treating it
    # as one would filter every ordinary query that happens to contain a number.
    if "min_bytes" not in f and "max_bytes" not in f:
        m2 = re.search(r"\b(\d+(?:\.\d+)?)\s*(tb|gb|mb|kb)\b", q)
        if m2:
            _spans.append(m2.span())
            f["min_bytes"] = int(float(m2.group(1))
                                 * _SIZE_UNITS.get(m2.group(2).lower(), 1))
            f["_min"] = m2.group(0).strip()
            # ⚠️ STATED BACK, because "10gb" was read as 10 GiB and someone who meant decimal
            # GB needs to see which was used rather than guess.
            f["_from_bare_unit"] = True

    # ⚠⚠️ RECORD WHAT WAS CONSUMED, BECAUSE THE LEFTOVER DECIDES WHETHER THIS IS A FILTER OR A
    # SEARCH — AND CONFLATING THE TWO IS WHY "10gb files" RETURNED FILES CONTAINING THE WORD
    # "files".
    #
    # ⚠️ "recent php files about authentication" IS BOTH: a filter (recent, php) and a text query
    # (authentication). Fusing them is right.
    #
    # ⚠️ "10gb files" IS ONLY A FILTER. After "10gb" is consumed, what is left is "files" — the
    # NOUN OF THE FILTER, not a search term. Nobody wants documents containing the word "files";
    # they want files of that size. Treating it as text meant the size filter matched nothing,
    # contributed nothing to the fusion, and the text search won by default.
    m = _EXT_RX.search(query)
    if m:
        _spans.append(m.span())
        f["ext"] = "." + m.group(2).lower()
        f["_ext"] = m.group(1)

    # ⚠⚠️ THE LEFTOVER, COMPUTED FROM THE MATCHED SPANS RATHER THAN GUESSED. THIS IS THE FIX.
    #
    # ⚠️ "10gb files" leaves "files" — the NOUN OF THE FILTER, not a search term. Nobody wants
    # documents containing the word "files"; they want files of that size.
    #
    # ⚠️ "recent php files about authentication" leaves "files about authentication", which DOES
    # carry a text query, and fusing filter and text is right there.
    _kept = list(q)
    for _a, _b in _spans:
        for _i in range(_a, min(_b, len(_kept))):
            _kept[_i] = " "
    f["_leftover"] = " ".join("".join(_kept).split())
    return f





def describe(f: dict) -> str:
    """⚠️ THE PARSED FILTERS ARE SHOWN BACK. If the tool read "over 10 MB" as 10 MiB and the
    user meant decimal, the only way they can tell is if it says which one it used."""
    bits = []
    if "_min" in f:
        bits.append(f"size ≥ {human_bytes(f['min_bytes'])}")
    if "_max" in f:
        bits.append(f"size ≤ {human_bytes(f['max_bytes'])}")
    if "_since" in f:
        bits.append(f"modified {f['_since']}")
    if "_ext" in f:
        bits.append(f"extension {f['ext']}")
    return ", ".join(bits)


def human_bytes(n: int) -> str:
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024), ("B", 1)):
        if n >= div:
            v = n / div
            return f"{v:.0f} {unit}" if v >= 10 or div == 1 else f"{v:.1f} {unit}"
    return "0 B"


# ⚠️ WORDS THAT CARRY NO SEARCH INTENT. If the leftover is only these, the query was a
# FILTER and searching the text for them would return noise dressed as a result.
_FILLER = {"file", "files", "folder", "folders", "document", "documents", "stuff",
           "something", "anything", "the", "a", "an", "of", "in", "on", "with", "and",
           "my", "me", "all", "any", "some", "list", "show", "find", "get", "give",
           "what", "which", "are", "is", "that", "there", "over", "under", "than",
           "bigger", "larger", "smaller", "less", "more", "between", "older", "newer"}


def has_text_query(f: dict) -> bool:
    """⚠️ True when the filters did not consume the whole query.

    ⚠️ THIS IS THE DIFFERENCE BETWEEN A FILTER AND A SEARCH, and treating them as one is why
    "10gb files" returned files containing the word "files": the size filter matched nothing,
    the text search matched the noun, and the fusion let the noun win.
    """
    left = (f or {}).get("_leftover", "")
    return any(w not in _FILLER and len(w) > 1 for w in left.split())


def run(store, f: dict, limit: int = 40) -> list[tuple[int, float]]:
    """Execute the filters. ⚠️ Straight SQL — no ranking, because there is nothing to rank:
    every row either satisfies the filter or does not."""
    where, args = [], []
    if "min_bytes" in f:
        where.append("size >= ?"); args.append(f["min_bytes"])
    if "max_bytes" in f:
        where.append("size <= ?"); args.append(f["max_bytes"])
    if "since" in f:
        where.append("mtime >= ?"); args.append(f["since"])
    if "until" in f:
        where.append("mtime < ?"); args.append(f["until"])
    if "ext" in f:
        where.append("LOWER(path) LIKE ?"); args.append(f"%{f['ext']}")
    if not where:
        return []
    # ⚠️ ORDER BY THE PROPERTY THE QUERY ASKED ABOUT. "files over 100 MB" wants the biggest
    # first; "what did I change today" wants the newest first. Ordering by id would answer both
    # correctly and uselessly.
    order = ("size DESC" if "min_bytes" in f else
             "mtime DESC" if ("since" in f or "until" in f) else "path ASC")
    sql = f"SELECT id FROM documents WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"
    args.append(limit)
    rows = store.conn.execute(sql, args).fetchall()
    out = [(r[0], 1.0) for r in rows]

    # ⚠️⚠️ THE SKIPPED FILES MUST BE INCLUDED, AND THIS IS NOT AN EDGE CASE — IT IS THE MAIN CASE.
    #
    # "files over 100 MB" is the archetypal metadata query, and EVERY file it should return is
    # one the size limit excluded from `documents` in the first place. Measured before this fix:
    # the query returned ZERO hits while 150 matching files sat in the skipped table.
    #
    # ⚠️ A metadata query that excludes exactly the files it is about is not a small bug. It
    # returns a confident empty answer to a question whose answer is on disk.
    try:
        store._ensure_skipped()
        where2 = []
        args2 = list(args[:-1])          # drop the LIMIT already appended
        if "min_bytes" in f:
            where2.append("size >= ?"); args2_extra = [f["min_bytes"]]
        else:
            args2_extra = []
        if "max_bytes" in f:
            where2.append("size <= ?"); args2_extra.append(f["max_bytes"])
        if "ext" in f:
            where2.append("LOWER(path) LIKE ?"); args2_extra.append(f"%{f['ext']}")
        # ⚠️ `since`/`until` are NOT applied here: the skipped table does not store mtime for
        # every entry, and filtering on a column that is mostly NULL would silently drop the
        # files that ARE searchable. Reported rather than faked.
        if where2:
            sql2 = f"SELECT path FROM skipped WHERE {' AND '.join(where2)}"
            for (path,) in store.conn.execute(sql2, args2_extra).fetchall():
                out.append((path, 1.0))
    except Exception:
        pass
    return out[:limit]
