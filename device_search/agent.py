"""
agent.py — the agentic part: read the query, choose the tools, merge, and explain.

===============================================================================
WHAT MAKES THIS "AGENTIC" AND NOT JUST "SEARCH MULTIPLE INDEXES"
===============================================================================
Running three backends and merging the results is not agentic — that is a fixed pipeline with
three stages. The agentic part is that **the query decides which tools run at all**, and that a
bad first pass changes the second:

    1. CLASSIFY   is this a code fragment, a filename, a phrase, or a question?
    2. ROUTE      pick the backends that can actually answer THAT kind of query
    3. OBSERVE    did the first pass find anything?
    4. REACT      if not, broaden — drop the exact requirement, fall back to meaning

⚠️ STEP 2 IS THE PART THAT MATTERS MOST, AND IT IS THE PART MOST TOOLS GET WRONG BY OMISSION.
Running embeddings on `InputLayer(shape=(784,))` returns plausible nonsense; running exact match
on "where do I configure the embedding dimension" returns nothing at all. **One of those two
queries is the user's own stated example, and it is the one embeddings fail.**

⚠️ THE CLASSIFIER IS RULE-BASED, NOT A MODEL. That is a deliberate limitation: it is
deterministic, testable, and needs no key. ⚠️ It is also genuinely worse than a model would be
on ambiguous queries, and saying so here is more useful than implying otherwise.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import find_secrets
from .crawl import find_line

# =============================================================================
# 1. CLASSIFY
# =============================================================================
# ⚠️ ORDER IS THE LOGIC. These are checked top to bottom and the FIRST match wins, so the most
# specific shapes have to come first: a quoted string that also looks like a question is a
# quoted string.
QUESTION_STARTS = ("what", "where", "which", "how", "why", "who", "when", "is ", "are ",
                   "does ", "do i", "can i", "find ", "locate ", "show me")
CODE_CHARS = set("(){}[]<>=;:_/\\$#@")
FILENAME_RX = re.compile(r"^[\w\-. ]+\.(py|js|ts|tsx|jsx|java|kt|go|rs|c|cpp|h|hpp|md|txt|"
                        r"json|yaml|yml|toml|ini|cfg|sh|sql|html|css|xml|csv|ipynb)$", re.I)


def classify(query: str) -> dict:
    """Return {'kind', 'backends', 'reasons'}.

    ⚠️ THE `reasons` LIST IS NOT DECORATION. It is what makes the routing auditable: when a
    search returns nothing, the first question is "did it even run the right backend?" and the
    answer has to be visible without reading the source.
    """
    q = query.strip()
    ql = q.lower()
    reasons: list[str] = []

    if len(q) >= 2 and q[0] == q[-1] and q[0] in "\"'":
        reasons.append("wrapped in quotes -> the user means the literal text")
        return {"kind": "literal", "backends": ["exact"], "reasons": reasons}

    if FILENAME_RX.match(q) and " " not in q:
        reasons.append("looks like a filename (has a known extension)")
        return {"kind": "filename", "backends": ["path", "keyword"], "reasons": reasons}

    # ⚠️ A CODE FRAGMENT IS DETECTED BY PUNCTUATION, NOT BY LENGTH. `x = 1` is 5 characters and
    # unambiguous; "how do I set x to 1" is the same question in prose and needs a different tool.
    has_code_punct = any(c in CODE_CHARS for c in q)
    looks_ident = bool(re.search(r"[A-Za-z_]\w*\.[A-Za-z_]\w*|[A-Za-z_]\w*\([^)]*\)", q))
    if has_code_punct or looks_ident:
        reasons.append("contains code punctuation or a call/attribute shape")
        # ⚠️ KEYWORD AS WELL AS EXACT: a fragment may have been reformatted by an editor and
        # no longer match byte-for-byte. Exact alone would report "not on disk", which is a lie.
        return {"kind": "code", "backends": ["exact", "keyword"], "reasons": reasons}

    if ql.startswith(QUESTION_STARTS):
        reasons.append("phrased as a question -> meaning matters more than wording")
        return {"kind": "question", "backends": ["semantic", "keyword"], "reasons": reasons}

    reasons.append("natural language with no code shapes -> try everything")
    return {"kind": "prose", "backends": ["semantic", "keyword", "exact", "path"],
            "reasons": reasons}


# =============================================================================
# 3. MERGE — Reciprocal Rank Fusion
# =============================================================================
def rrf(rank_lists: dict[str, list[tuple[int, float]]], k: int = 60) -> list[tuple[int, float]]:
    """⚠️ RRF, NOT SCORE SUMMATION, AND THE REASON IS CONCRETE: BM25 scores are unbounded and
    cosine similarities live in [-1, 1]. Adding them lets one backend's scale silently decide
    the ranking. RRF throws the scores away and uses only the ORDER, which is the one thing the
    backends agree on. Same k=60 as the RAG track, for the same reason."""
    fused: dict[int, float] = {}
    for _name, hits in rank_lists.items():
        for rank, (doc_id, _score) in enumerate(hits):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused.items(), key=lambda kv: -kv[1])


# =============================================================================
# 5. THE OPTIONAL LLM RERANK — and the interlock that guards it
# =============================================================================
@dataclass
class Candidate:
    doc_id: int
    path: str
    lang: str
    n_lines: int
    snippet: str = ""
    line_no: int | None = None
    score: float = 0.0
    sources: list[str] = field(default_factory=list)
    # ⚠️ Lexical (the user's words ARE in the file) vs semantic-only (closest by meaning).
    # Only the first is evidence that the thing exists.
    lexical: bool = False


def _client():
    """⚠️ Reads the key from the environment, never from a file in this repo. The key belongs
    in `.env` (gitignored) and is loaded by the CLI shell, exactly as the manual does it."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None, None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return (key, base, model), model


def llm_rerank(query: str, cands: list[Candidate], max_cands: int = 12,
               max_chars: int = 700) -> tuple[list[Candidate], dict]:
    """Ask a model which candidates actually answer the query.

    ⚠️⚠️ THE INTERLOCK IS THE IMPORTANT PART OF THIS FUNCTION, NOT THE RANKING.
    A device search that indexes `~` with no exclusions, paired with a reranker that ships
    snippets to an API, is one query away from uploading an SSH key. So:

        BEFORE ANY TEXT LEAVES THIS PROCESS, every snippet is scanned for recognised secret
        formats. A snippet that matches is DROPPED, not redacted-and-sent, and the drop is
        reported. ⚠️ Redaction is not enough here — a partially-redacted private key is still
        most of a private key, and I would rather return a worse ranking than upload one.

    ⚠️ THIS IS AN INTERLOCK, NOT A GUARANTEE. A regex cannot recognise an unlabelled password in
    a notes file. What it does is stop the catastrophic, obvious cases. **Anything a regex
    cannot recognise is the user's responsibility, and this docstring is where that is said.**
    """
    report = {"sent": 0, "blocked": 0, "blocked_kinds": [], "reason": ""}
    cls = _client()
    if cls[0] is None:
        report["reason"] = "no OPENAI_API_KEY set — skipped (this is not a failure)"
        return cands, report

    safe: list[Candidate] = []
    for c in cands[:max_cands]:
        hits = find_secrets(c.snippet)
        if hits:
            report["blocked"] += 1
            report["blocked_kinds"].extend(k for k, _ in hits)
            report["blocked_kinds"] = sorted(set(report["blocked_kinds"]))
            continue
        safe.append(c)

    if not safe:
        report["reason"] = "every candidate was blocked by the secret interlock"
        return cands, report

    payload = "\n\n".join(
        f"[{i}] {c.path}\n{c.snippet[:max_chars]}" for i, c in enumerate(safe))
    prompt = (
        "You rank file-search results. The user searched their own computer.\n"
        f"QUERY: {query}\n\nCANDIDATES:\n{payload}\n\n"
        "Return ONLY a JSON array of the candidate indices that genuinely answer or contain "
        "the query, best first. Omit any that are irrelevant. Example: [3,0,7]")

    try:
        import requests
        key, base, model = cls[0]
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0, "max_tokens": 200},
            timeout=60)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        report["sent"] = len(safe)
        m = re.search(r"\[[\d,\s]*\]", text)
        if not m:
            report["reason"] = f"model returned no index array ({text[:60]!r})"
            return cands, report
        order = json.loads(m.group(0))
        ranked = [safe[i] for i in order if isinstance(i, int) and 0 <= i < len(safe)]
        # ⚠️ APPEND WHAT THE MODEL OMITTED rather than dropping it. A model that returns 2 of 12
        # indices should reorder the list, not silently delete ten results the recall stage
        # worked to find. ⚠️ This is the single most common way an LLM reranker makes a search
        # tool WORSE: it optimises precision and nobody measures the recall it destroyed.
        rest = [c for c in safe if c not in ranked]
        return ranked + rest, report
    except Exception as e:
        report["reason"] = f"LLM call failed ({type(e).__name__}: {e}) — falling back to fusion order"
        return cands, report


# =============================================================================
# 2-4. THE LOOP
# =============================================================================
def search(query: str, store, semantic=None, use_llm: bool = False,
           top_k: int = 10, explain: bool = False) -> tuple[list[Candidate], dict]:
    """Classify -> route -> run -> observe -> broaden if empty -> merge -> optionally rerank."""
    plan = classify(query)
    trace = {"plan": plan, "backend_counts": {}, "broadened": False, "llm": {}}

    def run(backends: list[str]) -> dict[str, list[tuple[int, float]]]:
        out: dict[str, list[tuple[int, float]]] = {}
        for b in backends:
            if b == "exact":
                out[b] = store.exact_search(query, limit=40)
            elif b == "path":
                out[b] = store.path_search(query, limit=40)
            elif b == "keyword":
                out[b] = store.keyword_search(query, limit=40)
            elif b == "semantic":
                if semantic is None:
                    continue
                out[b] = semantic.search(query, limit=40)
            trace["backend_counts"][b] = len(out.get(b, []))
        return out

    results = run(plan["backends"])

    # ⚠️ STEP 4, THE REACT PART. If the routed backends found NOTHING, try the ones they skipped.
    # ⚠️ AND IT ONLY GOES ONE DIRECTION: it broadens, never narrows. A loop that can drop a
    # backend mid-query would make the tool's behaviour depend on its own earlier output in a way
    # that is impossible to reason about — the failure mode that makes agentic systems undebuggable.
    if not any(results.values()):
        extra = [b for b in ("exact", "keyword", "semantic", "path") if b not in plan["backends"]]
        trace["broadened"] = True
        trace["broadened_with"] = extra
        results.update(run(extra))

    fused = rrf(results)
    cands: list[Candidate] = []
    for doc_id, score in fused[:top_k * 3]:
        row = store.by_id(doc_id)
        if not row:
            continue
        _id, path, lang, n_lines, text = row
        # ⚠️ EVERY RESULT CARRIES ITS PROVENANCE. "found in this file" is a grep-shaped shrug;
        # "line 214" is an answer. And the snippet has to come from the file that MATCHED, not
        # from the first 200 characters, or the preview shows unrelated context.
        loc = find_line(text, query)
        srcs = [b for b, hits in results.items() if any(h[0] == doc_id for h in hits)]
        cand = Candidate(doc_id, path, lang, n_lines, score=score, sources=srcs)
        # ⚠️⚠️ THE TWO-TIER FLAG, AND IT IS THE FIX FOR A REAL BUG.
        # A control query of `ZZQX_NOT_IN_ANY_FILE_9931` — definitely not on disk — returned
        # THIRTEEN confident results, because the broaden step fell through to semantic and
        # semantic ALWAYS returns its top-k. Cosine similarity is relative; "top 10 of 10" is
        # not "found".
        #
        # ⚠️ AND A THRESHOLD CANNOT FIX IT. Measured scores on this corpus:
        #     real query   "how do I check whether video moves smoothly"   0.673
        #     real query   "where is the BM25 implementation"              0.632
        #     GARBAGE      "asdkjhqwlekjhasd qwoiuqwoiu"                   0.649
        #     GARBAGE      "ZZQX_NOT_IN_ANY_FILE_9931"                     0.615
        # ⚠️ **The best garbage query outscores the worst real one. The boundary is NEGATIVE.**
        # That is embedding anisotropy — bge-small maps everything into a narrow cone, so every
        # similarity lands in 0.5-0.7 whether or not the document is relevant.
        #
        # So the tool distinguishes two DIFFERENT claims instead of pretending they are one:
        #     LEXICAL  (exact/keyword/path) -> "your words are in this file"
        #     MEANING  (semantic only)      -> "this is the closest thing I have"
        # ⚠️ Only the first is evidence of existence. Those are not the same sentence and the
        # output must not present them as one list.
        cand.lexical = any(b in ("exact", "keyword", "path") for b in srcs)
        if loc:
            cand.line_no, cand.snippet = loc
        else:
            cand.snippet = text[:300].replace("\n", " ⏎ ")
        cands.append(cand)
        if len(cands) >= top_k:
            break

    if use_llm and cands:
        cands, llm_report = llm_rerank(query, cands)
        trace["llm"] = llm_report
    else:
        trace["llm"] = {"sent": 0, "blocked": 0, "reason": "not requested (--llm off)"}
    return cands, trace
