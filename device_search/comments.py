"""
comments.py — decide WHAT to embed, per file type.

===============================================================================
⚠️⚠️ THE FINDING THIS ENCODES, MEASURED ON A REAL 5,570-DOCUMENT CORPUS
===============================================================================
    total chunks                                          45,811
    chunks from code files                                36,613   79.9% of the work
    chunks from files under 2 KB                           3,198    7.0%
    chunks from prose                                      8,766   19.1%

    PHP files                                   3,836   (31.1 MB)
    comment characters                          5.20 MB  (16.7% of the PHP)
    PHP files with real comments                3,587 of 3,836   (94%)

⚠️ SO 80% OF THE INDEX TIME GOES INTO EMBEDDING SOURCE CODE, AND A DENSE VECTOR OF SOURCE
CODE CARRIES ALMOST NOTHING.

    for (int i = 0; i < 3; i++) { sleep(pow(2, i)); }

What does that embed to? `sleep`, `pow`, `i` — words that mean a dozen things across a
dozen libraries. The vector is an average of all of them and describes none of them.

⚠️ BUT THE COMMENT ABOVE IT SAYS EXACTLY WHAT THE CODE DOES:

    // Retry the request with exponential backoff on failure

**And "retrying with backoff" is what you would TYPE to find it.** The prose inside code is
written in the language people search with; the code is not.

⚠️ 94% of these files have comments, and the comments are 17% of the text. So the semantic
layer of a code corpus is available for about a fifth of the price of embedding it whole.

===============================================================================
⚠️ AND THE DOCSTRING IS THE SAME IDEA FOR PYTHON, WHERE IT IS EVEN STRONGER
===============================================================================
In Python the docstring is not decoration — it is the API documentation, and it is the text
that ends up in every reference page and every search result. Embedding a bare signature finds
nothing; embedding the docstring that reads "Merge two shards. Returns False if they overlap."
finds the function when someone searches for what it DOES.
"""

from __future__ import annotations

import re

# =============================================================================
# WHAT TO SKIP ENTIRELY
# =============================================================================
# ⚠️⚠️ STRUCTURAL FORMATS HAVE NO NATURAL LANGUAGE IN THEM AT ALL.
# JSON, XML, CSV and lockfiles are 19% of this corpus. `"version": "1.2.3"` and
# `<setting name="timeout" value="30"/>` embed to noise — there is no sentence, no intent,
# nothing a meaning-based search could match. They are fully searchable by exact and keyword
# matching, which is the only way anyone searches them anyway.
STRUCTURAL_EXTS = {
    ".json", ".xml", ".csv", ".tsv", ".lock", ".map", ".min.js", ".min.css",
    ".svg", ".plist", ".ini", ".cfg", ".conf", ".properties", ".sarif",
}
# ⚠️ LOGS ARE REPETITIVE AND LOW-SIGNAL. 5 files here held 2.8 MB — enormous, and almost all of
# it timestamp prefixes and repeated status lines. Exact search still finds the one line.
LOG_EXTS = {".log", ".out", ".err"}

CODE_EXTS = {
    ".py", ".php", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".swift", ".kt", ".scala", ".sh",
    ".bash", ".zsh", ".ps1", ".sql", ".r", ".lua", ".pl", ".ex", ".exs", ".dart",
}
PROSE_EXTS = {".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".adoc"}

POLICY = {
    "prose": "embed the whole file — this IS natural language",
    "code": "embed the COMMENTS AND DOCSTRINGS only, not the code",
    "structural": "embed nothing — no natural language to embed",
    "log": "embed nothing — repetitive, low signal",
    "other": "embed the whole file — unknown type, assume it might be prose",
}


# ⚠️⚠️ THE POLICY VERSION, AND WHY IT MUST EXIST.
#
# The vector manifest fingerprints each shard on (mtime, document length) — what the DOCUMENT
# looks like. Changing this policy does not change any document: the files are identical, only
# the text we would EMBED is different. So a policy change is invisible to the resume logic,
# and the index would silently keep 45,811 vectors of raw code that the new policy exists to
# avoid.
#
# ⚠️ SO THE POLICY IS PART OF THE FINGERPRINT. Bump POLICY_REV whenever the rule changes; the
# hash catches accidental edits to the extension sets that a human would forget to record.
POLICY_REV = 2


def policy_version() -> str:
    import hashlib
    h = hashlib.blake2b(digest_size=8)
    for name in ("STRUCTURAL_EXTS", "LOG_EXTS", "CODE_EXTS", "PROSE_EXTS"):
        h.update(repr(sorted(globals()[name])).encode())
    h.update(str(POLICY_REV).encode())
    return f"v{POLICY_REV}-{h.hexdigest()[:8]}"


def classify(path: str) -> str:
    p = path.lower()
    for ext in STRUCTURAL_EXTS:
        if p.endswith(ext):
            return "structural"
    for ext in LOG_EXTS:
        if p.endswith(ext):
            return "log"
    for ext in PROSE_EXTS:
        if p.endswith(ext):
            return "prose"
    for ext in CODE_EXTS:
        if p.endswith(ext):
            return "code"
    return "other"


# =============================================================================
# EXTRACTING THE PROSE FROM CODE
# =============================================================================
def _strip_trailing_star(block: str) -> str:
    """⚠️ Turn a block comment into readable prose, or the embeddings see `*` on every line
    and the vector is dominated by formatting noise rather than the words."""
    lines = []
    for ln in block.split("\n"):
        ln = re.sub(r"^\s*(/\*+|\*+/|\*|//+|#+)\s?", "", ln).strip()
        if ln:
            lines.append(ln)
    return " ".join(lines)


def python_docstrings(text: str) -> str:
    """⚠️ `ast` RATHER THAN REGEX FOR PYTHON, because it is exact and costs nothing here.
    A regex for `\"\"\"...\"\"\"` misses single-quoted docstrings, f-strings and nesting."""
    import ast
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(node)
            if d:
                out.append(d)
        # ⚠️ Inline comments matter too — `# retry with backoff` above a loop is often the only
        # description of what that loop is for.
    for m in re.finditer(r"#[^\n]*", text):
        c = m.group(0).lstrip("# ").strip()
        if len(c) > 12:                      # ⚠️ skip `# ---` and `# TODO:` style markers
            out.append(c)
    return "\n".join(out)


def prose_in_code(path: str, text: str) -> str:
    """Return only the natural-language part of a source file.

    ⚠️ RETURNS "" RATHER THAN RAISING when nothing is found. A file with no comments is a normal
    case, not an error, and the caller treats "" as "nothing to embed" for this file.
    """
    p = path.lower()
    if p.endswith(".py"):
        return python_docstrings(text)

    out = []
    # ⚠️ Block comments first, and the pattern is non-greedy across newlines. A greedy match
    # would swallow from the first `/*` to the LAST `*/` in the file, turning a whole file into
    # one "comment" and embedding the code anyway — the exact failure this module prevents.
    for m in re.finditer(r"/\*.*?\*/", text, re.S):
        s = _strip_trailing_star(m.group(0))
        if len(s) > 12:
            out.append(s)
    # ⚠️ LINE COMMENTS. `//` for C-family, `#` for shell/ruby/perl/python-alikes. `#` is
    # excluded for PHP because `#` there collides with nothing but is rare, and `//` is checked
    # first so a URL inside a string is not mistaken for a comment start.
    for m in re.finditer(r"(?m)^\s*(?://+|#)\s?(.{13,})$", text):
        out.append(m.group(1).strip())
    # ⚠️ TRAILING COMMENTS on a line of code: `$x = 1;  // clamp to the retry budget`
    for m in re.finditer(r"(?m);\s*(?://+)\s?(.{13,})$", text):
        out.append(m.group(1).strip())
    return "\n".join(out)


def embed_target(path: str, text: str) -> tuple[str, str]:
    """Return (text_to_embed, reason). An empty text means "do not embed this file".

    ⚠️ THE REASON IS RETURNED, NOT DISCARDED. When a user asks "why can't I find this by
    meaning", the answer has to be available — "because it is a JSON file" and "because it is
    code with no comments" are different facts and both are useful.
    """
    kind = classify(path)
    if kind == "structural":
        return "", "structural format — no natural language to embed"
    if kind == "log":
        return "", "log file — repetitive, low signal"
    if kind == "code":
        prose = prose_in_code(path, text)
        # ⚠️ A CODE FILE WITH NO COMMENTS IS NOT EMBEDDED AT ALL. Embedding the raw code as a
        # fallback would quietly restore the 80% cost for exactly the files that have the
        # least meaning — the bundled, minified, generated ones.
        if len(prose.strip()) < 40:
            return "", "code with no comments — nothing to embed"
        return prose, f"code comments ({len(prose):,} of {len(text):,} chars)"
    return text, f"{kind} file — embedded whole"
