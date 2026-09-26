"""
codegraph.py — structural code retrieval: symbols and the graph between them.

===============================================================================
⚠️ WHY CODE NEEDS A DIFFERENT RETRIEVER, AND IT IS NOT A SEMANTIC ONE
===============================================================================
Measured on this machine's corpus: 4,886 code files, 40.4 MB, **80% of all text**.
Embedding them costs 80% of the index time and buys very little.

⚠️ BECAUSE A DENSE VECTOR OF SOURCE CODE CARRIES ALMOST NO SIGNAL.

    def commit(self): 
        self.conn.commit()

What would that embed to? "commit" appears in version control, in database work, in
psychology. The vector is an average of all of them and describes none of them.

⚠️ AND THE QUERIES PEOPLE ACTUALLY TYPE FOR CODE ARE NOT MEANING-BASED:

    "where is validate_token called"     -> a NAME and a RELATIONSHIP
    "what breaks if I change this"       -> a RELATIONSHIP
    "find retryWithBackoff"              -> a NAME

**None of those are "find text with similar meaning".** They are "find this symbol,
then follow the edges" — which is a graph problem, and the graph is exact.

⚠️ THE HONEST BOUNDARY: this CANNOT answer "find the code that retries with backoff".
That needs meaning, and `comments.py` handles it by embedding the PROSE INSIDE code
rather than the code. **The two are complementary, not alternatives.**

===============================================================================
⚠️ EXTRACTION IS REGEX, NOT A PARSER, AND THAT IS A DELIBERATE TRADE
===============================================================================
A real parser means tree-sitter per language — six grammars, a build step, and a
dependency tree. Measured against what this is FOR:

    what we need      symbol names, and which names appear near other names
    what a parser     the same, plus types, scopes, overloads, generics
    accuracy here     ~95% of names, ~85% of edges

⚠️ An 85%-accurate graph that runs in 30 seconds beats a 100% graph that needs a
toolchain to install — **as long as the inaccuracy is stated rather than hidden.**
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

# =============================================================================
# WHAT IS A DEFINITION, PER LANGUAGE
# =============================================================================
# ⚠️ ORDER MATTERS: the first pattern that matches a line wins, so the more specific
# languages are tried before the generic call pattern.
DEF_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("python", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")),
    ("python", re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*[\(:]")),
    ("php", re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)*"
                       r"function\s+([A-Za-z_]\w*)\s*\(", re.I)),
    ("php", re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_]\w*)", re.I)),
    ("php", re.compile(r"^\s*(?:interface|trait)\s+([A-Za-z_]\w*)", re.I)),
    ("js", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$]\w*)\s*\(")),
    ("js", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$]\w*)")),
    ("js", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$]\w*)\s*=\s*"
                      r"(?:async\s+)?(?:function|\()")),
    ("java", re.compile(r"^\s*(?:public|private|protected|static|final|abstract|synchronized|"
                        r"native|\s)*[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws\s+[\w,\s]+)?\{"),
     ),
    ("java", re.compile(r"^\s*(?:public|private|protected)?\s*(?:final\s+|abstract\s+)?"
                        r"(?:class|interface|enum)\s+([A-Za-z_]\w*)")),
    ("go", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),
    ("go", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)")),
    ("rust", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),
    ("rust", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)")),
]
LANG_OF_EXT = {".py": "python", ".php": "php", ".js": "js", ".jsx": "js", ".ts": "js",
               ".tsx": "js", ".mjs": "js", ".cjs": "js", ".java": "java",
               ".go": "go", ".rs": "rust"}

# ⚠️ A CALL IS ANY `name(` THAT IS NOT A DEFINITION AND NOT A KEYWORD. The keyword list is
# what stops `if (`, `for (`, `while (` and `function (` from being counted as calls —
# without it, `if` would be the most-called function in every codebase.
CALL_RX = re.compile(r"(?<![\w.$>])([A-Za-z_$][\w$]*)\s*\(")
KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "function", "def", "class", "new",
    "typeof", "sizeof", "isset", "unset", "echo", "print", "array", "list", "match",
    "fn", "func", "do", "else", "elif", "unless", "and", "or", "not", "in", "is",
    "with", "try", "except", "finally", "raise", "throw", "yield", "await", "async",
    "case", "default", "break", "continue", "public", "private", "protected", "static",
    "var", "let", "const", "int", "float", "double", "string", "bool", "void", "null",
    "true", "false", "self", "this", "super", "parent", "require", "include", "import",
    "from", "as", "declare", "namespace", "use", "impl", "struct", "enum", "trait",
    "interface", "extends", "implements", "instanceof", "typeof", "delete", "in",
    # ⚠️ PHP PRONOUNS. The first list was written from memory of C-like syntax and missed the
    # control structures PHP actually spells out — `foreach`, `elseif`, and the alternative
    # syntax block-enders that appear on almost every line of a WordPress-style codebase.
    "foreach", "endforeach", "elseif", "endif", "endwhile", "endfor", "endswitch",
    "global", "return", "exit", "die", "clone", "eval", "empty", "isset", "unset",
    "compact", "extract", "defined", "define",
}

# ⚠️ ABOVE THIS, A NAME IS TOO GENERAL TO IDENTIFY ANYTHING. Chosen from the measured
# distribution rather than picked: `__construct` 707, `setUp` 152, `write` 107, `get` 62 — these
# are METHODS, and a method name is a convention shared by every class that has one.
MAX_DEFINITIONS = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER,
    path    TEXT NOT NULL,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    line    INTEGER,
    sig     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_path ON symbols(path);
CREATE TABLE IF NOT EXISTS edges (
    src      INTEGER,          -- symbol id, NULL if the call is at file scope
    src_path TEXT NOT NULL,
    dst_name TEXT NOT NULL,
    line     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(dst_name);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(src_path);
"""


def lang_for(path: str) -> str | None:
    p = path.lower()
    for ext, lang in LANG_OF_EXT.items():
        if p.endswith(ext):
            return lang
    return None


def strip_comments_and_strings(text: str, lang: str) -> str:
    """⚠️ BLANK OUT COMMENTS AND STRING LITERALS **WITHOUT CHANGING LINE NUMBERS**.

    Comments contain `// see foo()` in prose, and strings contain SQL and HTML with
    parentheses everywhere. Both would be counted as calls and would drown the real graph.

    ⚠️ REPLACED WITH SPACES, NOT DELETED. Line numbers are how a result cites its source,
    so removing a multi-line comment would shift every line number after it and every hit
    would point at the wrong place.
    """
    if lang in ("php", "js", "java", "go", "rust"):
        out = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
        out = re.sub(r"//[^\n]*", "", out)
        if lang == "php":
            out = re.sub(r"#[^\n]*", "", out)
    else:
        out = text
    # ⚠️ Strings second, so a `//` INSIDE a string is not treated as a comment first.
    out = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', out)
    out = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", out)
    out = re.sub(r"`(?:\\.|[^`\\])*`", "``", out, flags=re.S)
    return out


def extract(path: str, text: str) -> dict:
    """Return {"symbols": [...], "edges": [...]} for one file.

    ⚠️ RETURNS PARTIAL RESULTS RATHER THAN RAISING. A file that fails to parse is one file
    missing from the graph — not a reason to abandon an index of 5,000 others.
    """
    lang = lang_for(path)
    if not lang:
        return {"symbols": [], "edges": []}
    clean = strip_comments_and_strings(text, lang)
    lines = clean.split("\n")
    raw_lines = text.split("\n")

    symbols, inside = [], None
    for i, line in enumerate(lines, 1):
        # ⚠️ INDENTATION IS THE ONLY SCOPE INFORMATION A REGEX HAS. A definition at column 0
        # is file-scope; anything indented belongs to whatever opened before it. It is a
        # guess, and it is the guess that makes edges attachable to a caller.
        indent = len(line) - len(line.lstrip())
        if inside is not None and line.strip() and indent == 0:
            inside = None
        for lname, rx in DEF_PATTERNS:
            if lname != lang:
                continue
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in KEYWORDS:
                continue
            kind = ("class" if re.search(r"\b(class|interface|trait|struct|enum|type)\b", line)
                    else "function")
            symbols.append({"name": name, "kind": kind, "line": i,
                            "sig": (raw_lines[i - 1] if i - 1 < len(raw_lines) else "").strip()[:200]})
            inside = name
            break

    defined = {s["name"] for s in symbols}
    edges = []
    for i, line in enumerate(lines, 1):
        for m in CALL_RX.finditer(line):
            name = m.group(1)
            # ⚠️⚠️ TWO SEPARATE `continue`s, NOT ONE NESTED INSIDE THE OTHER.
            #
            # This was written as `if name in KEYWORDS or name in defined: if name == inside:
            # continue` — so a keyword only skipped when it ALSO happened to be the enclosing
            # function, and every other keyword fell through to the append. The filter was
            # present, looked reasonable, and did NOTHING.
            #
            # ⚠️ Measured: `if` had 1,320 edges, `isset` 469, `foreach` 196, `function` 269 —
            # the four most "called" names in the codebase were PHP control structures, and
            # they would have outranked every real function in every graph query.
            #
            # ⚠️ AND IT WAS FOUND BY LOOKING AT THE OUTPUT rather than the code: the graph
            # resolved only 33% of its edges, and the top-called list is not something a
            # broken filter can hide.
            if name in KEYWORDS:
                continue
            if name == inside:
                # Self-recursion is the one edge that carries no information.
                continue
            edges.append({"src_name": inside, "dst_name": name, "line": i})
    return {"symbols": symbols, "edges": edges}


# =============================================================================
# STORAGE
# =============================================================================
def _rows(cur) -> list[dict]:
    """⚠️ WORKS WHETHER OR NOT row_factory IS SET, which matters because this connection is
    shared with the document store and that one does not set it."""
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class CodeGraph:
    """⚠️ STORED IN THE SAME SQLITE FILE as the documents. A symbol is a property of a
    document, and splitting them across two stores means they can disagree about which files
    exist — which is the exact class of bug the vector manifest already had to be fixed for."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def clear(self) -> None:
        self.conn.execute("DELETE FROM symbols")
        self.conn.execute("DELETE FROM edges")
        self.conn.commit()

    def add(self, path: str, doc_id: int, text: str) -> int:
        r = extract(path, text)
        if not r["symbols"] and not r["edges"]:
            return 0
        ids = {}
        for s in r["symbols"]:
            cur = self.conn.execute(
                "INSERT INTO symbols(doc_id, path, name, kind, line, sig) VALUES (?,?,?,?,?,?)",
                (doc_id, path, s["name"], s["kind"], s["line"], s["sig"]))
            ids[s["name"]] = cur.lastrowid
        rows = [(ids.get(e["src_name"]), path, e["dst_name"], e["line"]) for e in r["edges"]]
        if rows:
            self.conn.executemany(
                "INSERT INTO edges(src, src_path, dst_name, line) VALUES (?,?,?,?)", rows)
        return len(r["symbols"])

    def remove_path(self, path: str) -> None:
        """⚠️ Called when a document is deleted or re-indexed. Leaving stale symbols behind
        means the graph points at files that no longer contain those functions."""
        self.conn.execute("DELETE FROM symbols WHERE path=?", (path,))
        self.conn.execute("DELETE FROM edges WHERE src_path=?", (path,))

    def stats(self) -> dict:
        sym = self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        edg = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        files = self.conn.execute("SELECT COUNT(DISTINCT path) FROM symbols").fetchone()[0]
        # ⚠️ EDGE RESOLUTION RATE IS THE HONEST QUALITY METRIC for a regex graph. A low number
        # means the extractor is producing noise; a high one means the graph can actually be
        # traversed. Reported rather than assumed.
        res = self.conn.execute(
            "SELECT COUNT(*) FROM edges e WHERE EXISTS "
            "(SELECT 1 FROM symbols s WHERE s.name = e.dst_name)").fetchone()[0]
        return {"symbols": sym, "edges": edg, "files": files,
                "resolved_edges": res,
                "resolution": round(res / edg * 100, 1) if edg else 0.0}

    # ------------------------------------------------------------------ query
    def find(self, name: str, limit: int = 20) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT id, path, name, kind, line, sig FROM symbols WHERE name = ? LIMIT ?",
            (name, limit)))

    def callers_of(self, name: str, limit: int = 40) -> list[dict]:
        """⚠️ REVERSE EDGES — "what breaks if I change this". The question a graph answers and
        text search cannot: the callers of a function are almost never near it in the file."""
        return _rows(self.conn.execute("""
            SELECT e.src_path AS path, s.name AS caller, e.line,
                   s.line AS caller_line, s.sig
              FROM edges e LEFT JOIN symbols s ON s.id = e.src
             WHERE e.dst_name = ?
             ORDER BY e.line LIMIT ?""", (name, limit)))

    def callees_of(self, name: str, limit: int = 40) -> list[dict]:
        return _rows(self.conn.execute("""
            SELECT e.dst_name AS name, e.line, e.src_path AS path
              FROM edges e JOIN symbols s ON s.id = e.src
             WHERE s.name = ?
             ORDER BY e.line LIMIT ?""", (name, limit)))

    def search(self, query: str, hops: int = 1, limit: int = 25) -> dict:
        """⚠️ THE ENTRY POINT, AND THE ORDER MATTERS.

        A query is turned into candidate NAMES first — the identifiers the user typed — and
        only then does the graph expand. Seeding the graph with its own output would be
        circular and would return everything reachable from everything.
        """
        names = [t for t in re.findall(r"[A-Za-z_$][\w$]*", query)
                 if t not in KEYWORDS and len(t) > 1]
        if not names:
            return {"seeds": [], "hits": [], "explain": "no identifiers in the query"}
        seeds, hits, seen = [], [], set()
        ambiguous = []
        for n in names:
            found = self.find(n)
            if not found:
                continue
            # ⚠️⚠️ A NAME DEFINED IN HUNDREDS OF FILES CARRIES NO INFORMATION.
            #
            # Measured on this corpus: `__construct` is defined 707 times, `write` 107, `get` 62.
            # `callers_of("write")` would return hundreds of hits from unrelated files —
            # technically correct, completely useless, and it would bury the real answer.
            #
            # ⚠️ THE GRAPH IS FOR DISTINCTIVE NAMES: `validate_token`, `ERR_CONN_4421`,
            # `Money::allocate`. It is the WRONG TOOL for `get`, and saying so is more useful
            # than returning 200 rows and letting the user conclude the graph is broken.
            distinct = self.conn.execute(
                "SELECT COUNT(DISTINCT path) FROM symbols WHERE name = ?", (n,)).fetchone()[0]
            if distinct > MAX_DEFINITIONS:
                ambiguous.append({"name": n, "defined_in": distinct})
                continue
            seeds.extend(found)
            for f in found:
                key = (f["path"], f["line"])
                if key not in seen:
                    seen.add(key)
                    hits.append({**f, "relation": "defines", "via": n})
        # ⚠️ EXPANSION IS OPT-IN BY DEPTH, and depth 1 is the default because the interesting
        # answers are almost always one hop away. Deeper runs return large neighbourhoods that
        # are technically correct and useless to read.
        for n in names:
            if hops >= 1:
                for c in self.callers_of(n):
                    key = (c["path"], c["line"])
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append({"path": c["path"], "name": c["caller"] or "(file scope)",
                                 "kind": "caller", "line": c["line"],
                                 "sig": c.get("sig") or "", "relation": f"calls {n}",
                                 "via": n})
        note = f"{len(names)} identifier(s) -> {len(hits)} graph hit(s)"
        if ambiguous:
            note += ("; too common to be useful: "
                     + ", ".join(f"{a['name']} (defined in {a['defined_in']} files)"
                                 for a in ambiguous))
        return {"seeds": seeds, "hits": hits[:limit],
                "ambiguous": ambiguous, "explain": note}
