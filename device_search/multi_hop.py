"""Multi-hop retrieval: follow what a result points at.

One pass answers "where is the config file". It cannot answer "what does the config
file say the database should be", because the first document names a second
document and nothing reads it.

The gap is not ranking. The retrieved document is correct and still insufficient.
So the second hop is driven by REFERENCES FOUND IN THE RESULTS, not by re-running
the same query harder:

    hop 1: "which document explains the retry policy"
            -> docs/retry.md ("...see config/limits.yaml for the values")

    hop 2: the first result names config/limits.yaml, which is indexed
            -> config/limits.yaml, now in context

    answer: a fact that was in neither document alone

WHAT COUNTS AS A REFERENCE

Only things that resolve to a real indexed file. A mention of "the config" is not
a reference - it is a phrase. A mention of a path that exists in the index is.
That check is what separates following a link from searching for one.

WHY NOT A SECOND LLM CALL

A model can be asked "what else do you need", and it will invent a plausible
filename. Here the reference must already be in the corpus, so following it is a
lookup rather than a guess, and a wrong hop is impossible - the worst case is
that nothing new is added.

Cost: each hop is a database lookup per extracted reference, capped.
"""

from __future__ import annotations

import re
from pathlib import Path

# A path-ish token: something with a slash, or a filename with an extension.
# Deliberately narrow. `re.findall` over a large document with a looser pattern
# returns sentences, and each one would become a lookup.
_PATH_RX = re.compile(
    r"(?:\.{0,2}/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]{1,8}\b"
)

# Files that are named constantly and referred to by everything. Following them
# adds noise to every hop and no information.
_COMMON = {
    "readme.md", "license", "licence", "package.json", "pyproject.toml",
    "setup.py", "requirements.txt", "makefile", "dockerfile", ".gitignore",
    "tsconfig.json", "cargo.toml", "go.mod", "pom.xml",
}

MAX_REFERENCES = 8


def extract_references(text: str, limit: int = MAX_REFERENCES) -> list[str]:
    """Candidate file references in a chunk of text, most specific first.

    Returns the raw strings found; resolving them against the index is the next
    step, because this function has no idea what exists on disk.
    """
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATH_RX.finditer(text or ""):
        ref = m.group(0).strip(".,;:()[]{}<>\"'`")
        if not ref or len(ref) < 4:
            continue
        base = ref.rsplit("/", 1)[-1].lower()
        if base in _COMMON:
            continue
        if ref in seen:
            continue
        seen.add(ref)
        found.append(ref)
        if len(found) >= limit:
            break
    return found


def resolve_references(store, refs: list[str], exclude_ids: set[int]) -> list[tuple[int, str]]:
    """Turn candidate strings into document ids that exist in the index.

    A reference resolves when a document's path ends with it. Suffix matching
    rather than exact matching because documents cite `config/limits.yaml` and the
    indexed path is `/Users/x/project/config/limits.yaml`.

    Short references are rejected: `a.py` would match dozens of files and add all
    of them, turning one hop into a corpus.
    """
    hits: list[tuple[int, str]] = []
    for ref in refs:
        if len(ref) < 8: # `x.py` matches far too much
            continue
        rows = store.conn.execute(
            "SELECT id, path FROM documents "
            "WHERE COALESCE(is_dir,0)=0 AND path LIKE ? LIMIT 4",
            ("%" + ref,)).fetchall()
        for doc_id, path in rows:
            if doc_id in exclude_ids:
                continue
            hits.append((doc_id, path))
            exclude_ids.add(doc_id)
    return hits


def follow(store, candidates, max_hops: int = 1):
    """Add documents that the current candidates refer to.

    `candidates` is the list the agent already produced. Returns
    (new_documents, trace) where new_documents is [(doc_id, path, via_path)].

    The trace records what was followed and what was skipped, because "the second
    hop added nothing" and "the second hop never ran" look identical in a result
    list and need different fixes.
    """
    trace = {"hop": 0, "followed": [], "unresolved": [], "skipped_common": 0}
    if not candidates or max_hops < 1:
        return [], trace

    seen_ids = {c.doc_id for c in candidates}
    seen_paths = {c.path for c in candidates}

    for hop in range(max_hops):
        refs: list[str] = []
        for c in candidates:
            # The path itself is a reference to the thing already read, so it is
            # excluded; only references to OTHER files matter.
            text = ""
            try:
                row = store.by_id(c.doc_id)
                text = (row[4] if row else "") or ""
            except Exception:
                continue
            for r in extract_references(text):
                if r not in refs:
                    refs.append(r)
            if len(refs) > 40:
                break

        if not refs:
            break

        resolved = resolve_references(store, refs, seen_ids)
        if not resolved:
            trace["unresolved"] = refs[:10]
            break

        trace["hop"] = hop + 1
        added = []
        for doc_id, path in resolved:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            added.append((doc_id, path))
        trace["followed"].extend(
            {"doc_id": d, "path": p} for d, p in added)
        if not added:
            break
        # Stop after the first productive hop. A second hop would read the
        # documents just added and follow what THOSE name, which is a chain
        # rather than a wider search - and on a codebase it walks into a vendor
        # tree within three steps. One hop covers the question this exists for
        # ("the document names another document"); more is a product decision
        # that needs a relevance signal this function does not have.
        #
        # NOTE: this loop had an `if False else candidates` line, which is dead
        # code that still executes its condition. Removed rather than left.
        break

    return [(d["doc_id"], d["path"]) for d in trace["followed"]], trace
