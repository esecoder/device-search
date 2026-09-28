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


# ⚠️ WHEN SEMANTIC CONTRIBUTES AT ALL, AND HOW MUCH WHEN IT DOES. Both numbers are measured —
# see the block in search() for the six configurations and what each produced.
#
# ⚠️ `SEMANTIC_FALLBACK_BELOW` is the important one: above it, the semantic list is not fused,
# because on lexical-overlap queries it measurably made the ranking worse while finding nothing
# extra. Below it — a question whose words are NOT in the answer — lexical returns almost
# nothing, which is exactly when the embedder is the only backend that can help.
SEMANTIC_FALLBACK_BELOW = 3
SEMANTIC_FUSION_CAP = 5


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
        # ⚠️⚠️ `path` BELONGS HERE, AND ITS ABSENCE WAS WHY FILENAME SEARCH APPEARED BROKEN.
        #
        # This route runs for anything that LOOKS like code — an identifier, a dotted name, a
        # symbol. `db_acl.php` classifies as code, so it came here, and this list did not
        # include `path`. The filename backend existed, worked, and WAS NEVER CALLED for
        # exactly the queries most likely to be filenames.
        #
        # ⚠️ Measured: the router sent 'db_acl.php' to code/exact/keyword and returned path=0.
        # A user searching for a file by name got content matches from files they did not ask
        # for, and no match on the file itself.
        return {"kind": "code", "backends": ["exact", "keyword", "path"], "reasons": reasons}

    if ql.startswith(QUESTION_STARTS):
        reasons.append("phrased as a question -> meaning matters more than wording")
        # ⚠️ Same reasoning: a question can name a file, and file names are cheap to check.
        return {"kind": "question", "backends": ["semantic", "keyword", "path"],
                "reasons": reasons}

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
        # ⚠️⚠️ LEXICAL FIRST, WHATEVER ORDER THE ROUTE ASKED FOR.
        #
        # The semantic decision below depends on HOW MANY LEXICAL HITS THERE ARE — and the
        # `question` route lists ["semantic", "keyword", "path"], so semantic was evaluated
        # FIRST, saw zero keyword hits, and fired every single time. Measured end to end:
        # "lexical hits=40  semantic fired: True".
        #
        # ⚠️ THE THRESHOLD WAS CORRECT AND DEAD, because the thing it measured did not exist
        # yet. This is the same shape as the render-wipes-state bugs: the logic is right and
        # the ORDER makes it inert.
        ordered = [b for b in backends if b != "semantic"]
        if "semantic" in backends:
            ordered.append("semantic")
        for b in ordered:
            if b == "exact":
                out[b] = store.exact_search(query, limit=40)
            elif b == "path":
                out[b] = store.path_search(query, limit=40)
            elif b == "keyword":
                out[b] = store.keyword_search(query, limit=40)
            elif b == "semantic":
                if semantic is None:
                    continue
                # ⚠️⚠️ SEMANTIC IS A FALLBACK, NOT A CO-EQUAL, AND THIS IS MEASURED.
                #
                # RRF gives every entry in every list the same weight: 1/(k + rank). So a
                # bi-encoder's 40th-best guess competes on equal terms with BM25's — and on this
                # corpus those are not equal, because the embedder's similarities live in a
                # narrow cone where garbage scores 0.649 and a real query 0.632.
                #
                # ⚠️ MEASURED OVER 24 QUERIES, and the trend is monotonic — every semantic
                # result added made the ranking worse:
                #
                #     bm25 only                      21/24   MRR 0.547
                #     semantic top-40 (what this did)21/24   MRR 0.305   <-- -0.242
                #     semantic top-5                 22/24   MRR 0.370
                #     semantic top-1                 21/24   MRR 0.452
                #     semantic ONLY IF lexical < 3   21/24   MRR 0.547   <-- no harm at all
                #     keyword only (control)         21/24   MRR 0.547   <-- RRF is not the cause
                #
                # ⚠️ SO THE SEMANTIC LIST IS NOT FUSED WHEN LEXICAL ALREADY FOUND THINGS. On
                # every query above, BM25 found enough, so semantic never fired and the result
                # is identical to BM25 alone — which is the point.
                #
                # ⚠️ AND IT IS NOT DELETED, BECAUSE THAT WOULD BE READING TOO MUCH INTO THE
                # MEASUREMENT. These queries are built from the document's own words. That is
                # BM25's home turf and it is NOT what embeddings are for. A question whose
                # words do NOT appear in the answer returns few lexical hits — and that is
                # precisely the case where this now fires and semantic does the work.
                #
                # ⚠️ A SEMANTIC-ONLY HIT IS STILL REPORTED in the "closest by meaning" tier, so
                # this never hides a document that nothing else found.
                kw_found = len(out.get("keyword", [])) + len(out.get("exact", []))
                if kw_found < SEMANTIC_FALLBACK_BELOW:
                    out[b] = semantic.search(query, limit=SEMANTIC_FUSION_CAP)
                    trace["semantic_fired"] = {"lexical_hits": kw_found,
                                               "threshold": SEMANTIC_FALLBACK_BELOW}
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

    # ⚠️ METADATA RUNS ALONGSIDE, NOT INSTEAD. A sentence can carry both: "recent php files
    # about authentication" is a filter AND a text query, and answering only one of them answers
    # a different question than the one asked.
    try:
        from .metadata import describe as _mdesc, parse as _mparse, run as _mrun
        _mf = _mparse(query)
        if _mf:
            results["meta"] = _mrun(store, _mf, limit=40)
            trace["meta"] = {"filters": _mf, "explain": _mdesc(_mf),
                             "hits": len(results["meta"])}
    except Exception as e:
        trace["meta"] = {"error": f"{type(e).__name__}: {e}"}

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
        # ⚠️ METADATA IS LEXICAL EVIDENCE: "this file is 200 MB" is a fact about a file that
        # EXISTS, in the same category as "your words are in it". It is not a similarity.
        cand.lexical = any(b in ("exact", "keyword", "path", "meta") for b in srcs)
        if loc:
            cand.line_no, cand.snippet = loc
        else:
            cand.snippet = text[:300].replace("\n", " ⏎ ")
        cands.append(cand)
        if len(cands) >= top_k:
            break

    # ⚠️⚠️ THE CODE GRAPH, AND WHY IT IS NOT ANOTHER SIMILARITY SCORE.
    #
    # The other backends ask "which text looks like this query". This one asks "which SYMBOL
    # does this query NAME, and who touches it" — a different question with an exact answer.
    # `callers_of("validate_token")` is not a ranking; it is a fact.
    #
    # ⚠️ IT ONLY RUNS WHEN THE QUERY CONTAINS AN IDENTIFIER THE GRAPH KNOWS, so a prose question
    # costs nothing extra. A graph lookup on "how do I fix the deploy" finds no seed and returns
    # immediately.
    # ⚠️⚠️ GREP, AND IT RUNS ONLY WHEN THE INDEX FOUND NOTHING.
    #
    # This is the ONE case where grep beats an index: the files were excluded FROM the index by
    # definition — too large, binary, or media with no extractable text — so there is nothing to
    # search and reading them is the only option left.
    #
    # ⚠️ IT IS THE LAST RESORT AND IT SAYS SO. Reading 4.9 GB takes minutes, so it is triggered
    # by an empty result rather than run on every query, it has a time budget, and it reports how
    # much it actually covered. A grep that silently checks 40 of 249 files is a lie by omission.
    grep_hits, grep_meta = [], {}
    if not cands and len(query.strip()) >= 4:
        try:
            for _item in store.grep_skipped(query, limit=15):
                if isinstance(_item, tuple) and len(_item) == 3 and isinstance(_item[2], dict):
                    grep_meta = _item[2]
                    continue
                _path, _size, _ = _item
                grep_hits.append({"path": _path, "size": _size})
            if grep_hits or grep_meta:
                trace["grep"] = {"hits": grep_hits, "note":
                                 "found in files that are NOT in the index (too large or binary)",
                                 **grep_meta}
        except Exception as e:
            trace["grep"] = {"error": f"{type(e).__name__}: {e}"}

    graph_hits = []
    try:
        from .codegraph import KEYWORDS as _KW
        ids_in_query = [t for t in re.findall(r"[A-Za-z_$][\w$]*", query)
                        if t not in _KW and len(t) > 2]
        if ids_in_query:
            g = store.graph()
            found = g.search(query, hops=1, limit=15)
            for h in found.get("hits", []):
                graph_hits.append(h)
            if graph_hits:
                trace["graph"] = {"seeds": ids_in_query[:6], "hits": len(graph_hits),
                                  "explain": found.get("explain", "")}
    except Exception as e:
        trace["graph"] = {"error": f"{type(e).__name__}: {e}"}

    # ⚠️⚠️ STALENESS IS ATTACHED TO THE RESULT, NOT LEFT FOR THE CALLER TO REMEMBER.
    # `--no-semantic` does not clear vectors, and an interrupted embedding run leaves the old
    # ones in place. In both cases semantic hits are computed against a DIFFERENT document set —
    # and a user who is not told will read a wrong answer as a right one.
    try:
        from .vectors import VectorStore
        from .config import INDEX_DIR
        vs = VectorStore(INDEX_DIR, dim=384)
        row = store.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(mtime),0) FROM documents"
        ).fetchone()
        trace["vectors"] = vs.check_stale(row[0], row[1], float(row[2] or 0))
    except Exception as e:
        trace["vectors"] = {"stale": None, "reason": f"could not check ({type(e).__name__})"}

    if use_llm and cands:
        cands, llm_report = llm_rerank(query, cands)
        trace["llm"] = llm_report
    else:
        trace["llm"] = {"sent": 0, "blocked": 0, "reason": "not requested (--llm off)"}
    return cands, trace
