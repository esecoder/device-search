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


# ⚠️ HOW MANY SEMANTIC RESULTS ENTER THE FUSION. Measured on BOTH query types — see the block
# in search() for the table and for why no threshold can replace it.
#
# ⚠️ THIS IS A COMPROMISE, NOT A SOLUTION, AND THE EVIDENCE SAYS SO:
#     paraphrases (12% word overlap): top-5 finds 5/20, top-40 finds 5/20, none finds 3/20
#     lexical      (~50% overlap):    top-5 MRR 0.370, top-40 MRR 0.305, none 0.547
# It gains 2 of 20 on one query type and costs 0.18 MRR on the other, and nothing available
# distinguishes them. The real bottleneck is the EMBEDDER — 35% recall on the questions it
# exists for — not the fusion arithmetic.
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
# ⚠️ HOW MANY EACH BACKEND MAY OFFER TO THE FUSION. Not a display limit — the interface
# paginates — but the BM25 and vector scans are O(corpus) and an unbounded list per backend
# would make every keystroke pay for results nobody scrolls to.
PER_BACKEND = 400


def rrf(rank_lists: dict, k: int = 60, weights: dict | None = None) -> list[tuple]:
    """⚠️ RRF, NOT SCORE SUMMATION, AND THE REASON IS CONCRETE: BM25 scores are unbounded and
    cosine similarities live in [-1, 1]. Adding them lets one backend's scale silently decide
    the ranking. RRF throws the scores away and uses only the ORDER, which is the one thing the
    backends agree on. Same k=60 as the RAG track, for the same reason."""
    # ⚠️ `weights` LETS A BACKEND SAY "MY ORDER MATTERS MORE" WITHOUT A SECOND FUSION PATH.
    # ⚠️ A metadata filter is a truth condition, not a similarity: every row either matches or
    # does not, so its first entry is a certainty where a semantic top-1 is a guess. Weighting
    # lifts it above the noise without deleting the noise, which is the difference between
    # ranking something first and pretending it is the only thing.
    w = weights or {}
    fused: dict = {}
    meta: dict = {}
    for _name, hits in rank_lists.items():
        _w = float(w.get(_name, 1.0))
        for rank, h in enumerate(hits):
            # ⚠️ BOTH SHAPES ARRIVE HERE. Text backends yield (doc_id, score); metadata now yields
            # (doc_id, path, score) because a skipped file HAS no doc_id. The key is the doc_id
            # when there is one and the path when there is not.
            if len(h) == 3:
                _id, _path, _score = h
            else:
                _id, _score = h
                _path = None
            key = _id if _id is not None else _path
            fused[key] = fused.get(key, 0.0) + _w / (k + rank + 1)
            meta.setdefault(key, {"doc_id": _id, "path": _path})
    return [(k2, meta[k2], v) for k2, v in sorted(fused.items(), key=lambda kv: -kv[1])]


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
                # ⚠️ THE LIMIT IS THE PAGE, NOT THE ANSWER. Asking for 40 per backend meant the
                # fusion could only ever see 40, so an answer at rank 41 did not exist. ⚠️ The user
                # asked to see everything and to paginate rather than truncate - so the backends
                # return what they have and the interface decides what to DRAW.
                # ⚠⚠️ THE EXACT BACKEND IS FOR FRAGMENTS. RUNNING IT ON EVERYTHING COST 4.4 SECONDS.
                #
                # ⚠️ MEASURED AT 859,569 DOCUMENTS: `exact` was 4.39s of a 6.1s search — a LIKE
                # '%...%' over 6 GB of text. The docstring has always said so:
                #
                #     "LIKE '%...%' IS A FULL SCAN and that is accepted ... The alternative — a
                #      suffix automaton or n-gram inverted index — is the correct engineering
                #      answer at 100x this corpus and the wrong one for a first version."
                #
                # ⚠️ WE ARE NOW AT 100x THAT CORPUS. The docstring named the threshold and we
                # crossed it. (Trigram FTS5 is the answer for the fragments; it is measured and
                # queued, and it needs a nine-minute build.)
                #
                # ⚠️ BUT THE SCAN IS ONLY NEEDED FOR WHAT IT IS FOR. `exact` exists because
                # `InputLayer(shape=(784,))` is not a concept — it is 24 characters that exist
                # or do not. ⚠️ A QUERY OF ORDINARY WORDS IS ALREADY ANSWERED BY `keyword`, which
                # now runs on FTS5 in milliseconds, so scanning 6 GB to re-find the same rows is
                # paying the most expensive backend for the least information.
                #
                # ⚠️ SO IT RUNS WHEN THE QUERY LOOKS LIKE A FRAGMENT: punctuation, an
                # extension, a quoted string, an identifier. "screenshot" does not and skips it;
                # "InputLayer(shape=(784,))" and "db_acl.php" do and get it.
                # ⚠⚠️ AND NOT FOR A FILENAME EITHER, WHICH IS THE OTHER COMMON CASE.
                #
                # ⚠️ MEASURED: "db_acl.php" took 7.35s, almost all of it `exact` scanning 6 GB for
                # a string that `path_search` had already found in 0.73s. ⚠️ A query that names a
                # file is answered by the name, and the extension is what says so.
                #
                # ⚠️ SO THE TEST IS "IS THIS A FRAGMENT", NOT "DOES IT CONTAIN PUNCTUATION". A
                # filename contains a dot; a code fragment contains brackets, quotes, operators.
                # The dot is the one piece of punctuation that means the answer is a path.
                _is_name = bool(re.match(r"^[\w\-]+\.[A-Za-z0-9]{1,6}$", query.strip()))
                _frag = (bool(re.search(r"[^\w\s.]", query)) and not _is_name)
                if not _frag:
                    # ⚠️ AN EMPTY LIST, NOT A MISSING KEY. The fusion iterates the backends that
                    # ran; a missing key would make "exact" look like a backend that failed.
                    out[b] = []
                else:
                    out[b] = store.exact_search(query, limit=PER_BACKEND)
            elif b == "path":
                out[b] = store.path_search(query, limit=PER_BACKEND)
            elif b == "keyword":
                out[b] = store.keyword_search(query, limit=PER_BACKEND)
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
                # ⚠️⚠️ THE THRESHOLD THAT WAS HERE IS GONE, BECAUSE IT NEVER FIRED.
                #
                # It gated semantic on `len(keyword_hits) < 3`. But BM25 returns top-k
                # REGARDLESS OF SCORE, so a query with no relevance at all still returns 40
                # results and the guard never opened — including on the paraphrase queries it
                # was written for. Measured: fused == bm25 alone on that set, exactly.
                #
                # ⚠️ AND THE OBVIOUS REPLACEMENT ALSO FAILS. Gating on BM25's top SCORE looks
                # right and is not, because the score does not separate the two query types:
                #
                #     paraphrase queries   top score: median 25.13, max 39.93
                #     lexical queries      top score: median 19.17, max 51.42
                #
                # ⚠️ THE PARAPHRASE QUERIES SCORE HIGHER. A BM25 score is a property of how
                # often the query's words occur, and a paraphrase query still contains words
                # that occur somewhere — just not in the answer. So it cannot tell "the answer
                # is here" from "these words are common".
                #
                # ⚠️ SO SEMANTIC IS ALWAYS INCLUDED, CAPPED. The cap is where the two
                # measurements meet, and it is a compromise rather than a solution:
                #
                #     paraphrase set:  top-40 5/20   top-10 6/20   top-5 5/20   top-3 3/20
                #     lexical set:     top-40 MRR 0.305   top-5 MRR 0.370   none 0.547
                #
                # ⚠️ IT HELPS ON ONE AND HURTS ON THE OTHER, and neither threshold can tell them
                # apart. 5 is the smallest cap that keeps the paraphrase gain.
                out[b] = semantic.search(query, limit=SEMANTIC_FUSION_CAP)
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
        from .metadata import (describe as _mdesc, has_text_query as _mtext,
                               parse as _mparse, run as _mrun)
        _mf = _mparse(query)
        if _mf:
            results["meta"] = _mrun(store, _mf, limit=PER_BACKEND)
            trace["meta"] = {"filters": _mf, "explain": _mdesc(_mf),
                             "hits": len(results["meta"])}

            # ⚠⚠️ AND IF THE FILTERS CONSUMED THE WHOLE QUERY, THE TEXT RESULTS ARE DROPPED.
            #
            # ⚠️ THIS IS THE BUG THE USER FOUND. "10gb files" parsed to a size filter, and the
            # remaining word "files" was searched as TEXT — so files containing the word "files"
            # came back for a question about size. The filter matched nothing, contributed nothing
            # to the fusion, and the text search won by default.
            #
            # ⚠️ NOBODY WANTS DOCUMENTS CONTAINING THE WORD "files". They want files of that
            # size. "10gb files" is a FILTER, not a search, and the two must not be fused as if
            # the leftover were a query.
            #
            # ⚠️ THE OTHER DIRECTION STILL WORKS: "recent php files about authentication" leaves
            # "files about authentication", which carries real words, so the text backends stay
            # and both run. The difference is decided by has_text_query(), from the spans each
            # filter actually matched — not by a guess about which words look like noise.
            # ⚠⚠️ NOTHING IS DROPPED. THE FILTER'S ANSWER IS RANKED FIRST, AND THE REST STILL
            # COMES BACK — which is what the user asked for and what is right.
            #
            # ⚠️ The first version DELETED the text results when the filter consumed the query.
            # That over-corrects: someone searching "10gb files" may still want the file called
            # "10gb-notes.txt", and silently removing results is its own kind of lie — the same
            # failure as returning noise, in the opposite direction.
            #
            # ⚠️ THE REAL PROBLEM WAS ORDER, NOT PRESENCE. A filter is not a ranked list — every row
            # either satisfies it or does not — so it should not compete on equal terms with a
            # similarity score. It gets a WEIGHT instead, which lifts it without deleting anything.
            if not _mtext(_mf):
                trace["meta"]["pure_filter"] = True
                # ⚠⚠️ A PURE FILTER THAT MATCHED NOTHING MUST NOT FALL BACK TO TEXT.
                #
                # ⚠️ "10gb files" has no file over 10 GB. The leftover word is "files", and
                # searching for it returns PhoneNumberMetadata_GB.php — noise dressed as an
                # answer to a question about size. The user sees results and assumes they are
                # relevant, which is worse than seeing none.
                #
                # ⚠️ AN EMPTY FILTER RESULT IS A REAL ANSWER: there are no files that big. The
                # caller is told why so it can say so, instead of being handed the word "files".
                if not results.get("meta"):
                    for _b in ("semantic", "keyword", "exact", "path"):
                        results.pop(_b, None)
                    trace["meta"]["empty_is_the_answer"] = True
    except Exception as e:
        trace["meta"] = {"error": f"{type(e).__name__}: {e}"}

    # ⚠️ THE FILTER IS WEIGHTED, NOT PREFERRED BY DELETION. 6.0 lifts a matched filter above the
    # similarity lists while leaving every one of them in the fusion.
    # ⚠⚠️ A NAME MATCH IS EVIDENCE, NOT A SIMILARITY, AND IT WAS BEING OUTVOTED BY VOLUME.
    #
    # ⚠️ MEASURED: path_search found the Screenshots folders with the HIGHEST raw score in the
    # whole pipeline (3.0 against keyword's floor), and NOT ONE reached the user. RRF discards
    # scores and counts lists, so a doc found by `path` alone scored 1/(60+0+1) = 0.0164 while a
    # doc found by `keyword` AND `exact` scored 0.033.
    #
    # ⚠️ FOUR BACKENDS × 40 HITS = 160 DOCUMENTS FOR 30 SLOTS. The folder you named ranks below
    # the cutoff and is cut, by documents that merely CONTAIN the word.
    #
    # ⚠️ SOMEONE WHO TYPES "screenshot" AND HAS A FOLDER CALLED "Screenshots" HAS ALREADY TOLD YOU
    # THE ANSWER. A name matching a query is close to a truth condition; a similarity score is a
    # guess. So `path` is weighted like the filter is.
    _weights = {"meta": 6.0} if trace.get("meta", {}).get("pure_filter") else {"meta": 2.5}
    _weights["path"] = 4.0
    fused = rrf(results, weights=_weights)

    # ⚠⚠️ AND THEN INTERLEAVE, BECAUSE A WEIGHT THAT PUTS THE RIGHT RESULT FIRST ALSO LETS IT OWN
    # THE WHOLE PAGE.
    #
    # ⚠️ MEASURED, AND THE USER REPORTED IT TWICE WITHOUT KNOWING IT WAS ONE THING: "screenshot"
    # returned EVERY result tagged `path`, and "1mb files" returned every result tagged `meta`.
    # Both were the weight doing its job too well — meta at 6.0 lifted all 40 filtered files
    # above every text match, and path at 4.0 lifted every name match above every content match.
    #
    # ⚠️ THE TAGS WERE CORRECT THE WHOLE TIME. The distribution was not. Someone searching
    # "screenshot" may also want the file that mentions screenshots in its content, and someone
    # searching "1mb files" may also want the file called "1mb-notes.txt" — and neither existed
    # in the top 25 because a single backend had taken all of it.
    #
    # ⚠️ THE OWNER OF THE LIST IS THE BACKEND WITH THE HIGHEST WEIGHT, so it goes first — once —
    # and then one from each other backend that has anything left, and so on. The weighted order
    # is preserved WITHIN each backend, so the best metadata result still leads.
    #
    # ⚠️ RESULTS FOUND BY SEVERAL BACKENDS ARE KEPT WHERE THEY ARE. Agreement is real evidence,
    # and round-robining them away would trade one distortion for another.
    _by_backend: dict = {}
    for _b, _hits in results.items():
        if not _hits:
            continue
        for _r, _h in enumerate(_hits):
            _id, _path, _wx = (_h[0], _h[1], _h[2]) if len(_h) == 3 else (_h[0], None, _h[1])
            _by_backend.setdefault(_b, []).append(_id if _id is not None else _path)

    # ⚠⚠️ ORDER BY WHAT WAS ASKED, THEN BLEND THE REST. THE ROUND-ROBIN WAS OVER-CORRECTING.
    #
    # ⚠️ The first version interleaved strictly, one from each backend in turn. That fixed "every
    # result is tagged path" by defeating the ordering: a query about a FOLDER put a keyword
    # match second, ahead of the second-best folder. ⚠️ The user's words: "include every matches
    # but ordering should not sacrificed."
    #
    # ⚠️ SO THERE ARE TWO REGIONS, NOT ONE SEQUENCE:
    #
    #     1. THE PRIMARY BACKEND'S RESULTS, in their own order — a folder query leads with
    #        folders, a size query leads with the sizes that matched.
    #     2. THEN EVERYTHING ELSE, interleaved — still blended, because "the file that mentions
    #        screenshots" belongs in the list, just not above the folder called Screenshots.
    #
    # ⚠️ WHAT DECIDES PRIMARY IS THE QUERY ITSELF, not a fixed preference: the filters if there
    # are filters, the route if the words look like a name, and the text backends otherwise.
    _kind = trace.get("plan", {}).get("kind")
    _meta = trace.get("meta", {})
    # ⚠⚠️ A STRONG NAME MATCH IS THE INTENT, WHATEVER THE ROUTER CALLED THE QUERY.
    #
    # ⚠️ The router classifies "screenshot" as `prose` — one word, no punctuation, nothing that
    # says "filename" to a regex. So keyword took the lead and the user, who meant the FOLDER,
    # got documents containing the word first.
    #
    # ⚠️ path_search already knows the difference: 12.0 for a folder named exactly the query, 6.0
    # for a file, 3.0 for a name that starts with it, 2.0 for one that contains it. ⚠️ ANYTHING
    # ABOVE A MERE CONTAINMENT MEANS THE USER NAMED A THING. That is better evidence than the
    # route, because it comes from what is actually on disk.
    _path_top = max((sc for _i, sc in results.get("path", [])), default=0.0)
    if _meta.get("pure_filter"):
        _primary = "meta"          # "1mb files" - the answer is the set that matched
    elif _path_top >= 3.0:
        _primary = "path"          # ⚠️ a real name match outranks any route guess
    elif _kind == "filename":
        _primary = "path"          # "where is config.py"
    elif _kind == "code":
        _primary = "exact"         # an identifier: exact symbol beats a fuzzy name
    else:
        _primary = "keyword"       # prose: the words are the evidence
    # ⚠️ IF THE PRIMARY FOUND NOTHING, the next-strongest takes the lead rather than leaving
    # the top of the list to whatever happens to be first in the dict.
    if not _by_backend.get(_primary):
        _primary = max(_by_backend, key=lambda b: _weights.get(b, 1.0)) if _by_backend else None
    trace.setdefault("meta", {})["primary"] = _primary

    _seen: set = set()
    _interleaved: list = []
    # ⚠️ REGION 1: the primary backend, untouched.
    for _id in _by_backend.get(_primary, []):
        if _id not in _seen:
            _seen.add(_id)
            _interleaved.append(_id)
    # ⚠️ REGION 2: everything else, interleaved by weight.
    _order = sorted((b for b in _by_backend if b != _primary),
                    key=lambda b: -_weights.get(b, 1.0))
    _cursors = {b: 0 for b in _order}
    _total = sum(len(v) for v in _by_backend.values())
    while len(_interleaved) < _total:
        _progress = False
        for _b in _order:
            _lst, _i = _by_backend[_b], _cursors[_b]
            while _i < len(_lst) and _lst[_i] in _seen:
                _i += 1
            _cursors[_b] = _i
            if _i < len(_lst):
                _seen.add(_lst[_i])
                _interleaved.append(_lst[_i])
                _cursors[_b] = _i + 1
                _progress = True
        if not _progress:
            break
    # ⚠️ ANYTHING THE ROUND-ROBIN DID NOT REACH — results that only exist in the fused list,
    # such as a doc_id the backend lists do not carry — is appended in weighted order rather
    # than dropped. A blend that silently loses rows is worse than a lopsided one.
    for _k, _info, _sc in fused:
        if _k not in _seen:
            _interleaved.append(_k)
            _seen.add(_k)
    _rank = {k: i for i, k in enumerate(_interleaved)}
    fused = sorted(fused, key=lambda t: _rank.get(t[0], 10 ** 9))
    cands: list[Candidate] = []
    # ⚠⚠️ AND THE CUTOFF IS MUCH LARGER THAN top_k * 3, FOR THE SAME REASON.
    # 30 slots for the output of four backends means the fusion decides by VOLUME rather than by
    # agreement: any list that returns more rows crowds out the others. ⚠️ The fused ranking is
    # already weight-aware, so there is nothing to protect by truncating early.
    # ⚠⚠️ AND NOTHING IS CUT HERE AT ALL. This slice was the last place a real result could
    # disappear without the user being told, and "we found it but did not show it" is the one
    # outcome a search tool must never produce silently.
    for _key, _info, score in fused:
        doc_id, path = _info["doc_id"], _info["path"]
        # ⚠⚠️ A RESULT WITH NO ROW IS NOT A RESULT TO DISCARD — IT IS A SKIPPED FILE.
        #
        # ⚠️ These are the files the size limit kept OUT of `documents`, and they are the entire
        # answer to "files over 100 MB". Discarding them because store.by_id() returns None is
        # what made every metadata query silently empty.
        if doc_id is None:
            from .metadata import human_bytes
            _p = Path(path) if path else None
            lang = (_p.suffix.lstrip(".").lower() if _p else "") or "file"
            try:
                _st = _p.stat() if _p and _p.exists() else None
                n_lines = 0
                _why = "not indexed (too large or binary)"
            except OSError:
                _st, n_lines, _why = None, 0, "not indexed"
            # ⚠️ doc_id -1 MARKS "NOT IN THE INDEX" — a skipped file, which is what a size
            # query returns, NOT a directory. Directories have real ids because they are indexed.
            cands.append(Candidate(
                -1, path or "", lang, n_lines, score=score,
                snippet=f"{human_bytes(int(_st.st_size)) if _st else ''} — {_why}".strip(" —"),
                sources=["meta"], lexical=True))
            continue
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
