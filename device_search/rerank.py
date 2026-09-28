"""
rerank.py — a cross-encoder that reorders results the first-stage retriever already found.

===============================================================================
⚠️⚠️ WHY THIS IS A SEPARATE STAGE AND NOT A BETTER EMBEDDING
===============================================================================
The first stage is a BI-encoder: query and document are embedded SEPARATELY and compared by
cosine. That is the only way to search 5,570 documents in 60 ms, because every document
vector can be computed once, at index time, and never looked at again.

⚠️ AND IT IS WHY THE ORDER IS APPROXIMATE. The query never sees the document; both are
compressed into 384 numbers independently, and any detail that does not survive that
compression cannot influence the ranking.

A CROSS-encoder reads (query, document) TOGETHER, in one forward pass, and outputs a single
relevance score. It sees the actual words, the actual order, and the actual relationship.

⚠️ THE COST IS THE REASON IT CANNOT BE THE FIRST STAGE: it is one forward pass PER CANDIDATE.
Re-ranking 50 candidates means 50 passes. Re-ranking a corpus means never finishing.

    stage 1   bi-encoder   ~60 ms over the whole corpus   ->  top 50 candidates
    stage 2   cross-encoder  ~5-8 s over those 50         ->  the same 50, reordered

⚠️ SO IT IS OPT-IN, NEVER AUTOMATIC. Spending eight seconds to reorder a list that already
appeared is a choice the user makes, not a default that makes every search feel broken.

===============================================================================
⚠️ WHAT IT CANNOT DO, STATED HERE SO NOBODY EXPECTS OTHERWISE
===============================================================================
A re-ranker REORDERS. It does not add results, and it does not decide relevance from nothing.
If the correct document is not in the candidate list, no amount of re-ranking will find it.

⚠️ Measured on this corpus, the first-stage weakness is not ordering but ANISOTROPY — bge-small
maps everything into a narrow cone, so a garbage query scores 0.649 while a real one scores
0.632. **A correct candidate list with a perfect order is still a correct candidate list with
a bad boundary.** Re-ranking improves the top of a list that is already right; it does not
fix a list that is wrong.
"""

from __future__ import annotations

import time

DEFAULT_MODEL = "BAAI/bge-reranker-base"

# ⚠️ THE CANDIDATE CAP, AND IT IS THE WHOLE DESIGN. The model is O(n) forward passes. At
# roughly 8 candidates per second on this CPU, 50 candidates is ~6 seconds and 200 would be
# half a minute — and the improvement is concentrated in the top of the list anyway, because
# the first stage is much better at "roughly relevant" than at "exactly this one".
DEFAULT_TOP_N = 20


def available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
    except Exception as e:
        return False, f"fastembed has no cross-encoder support ({type(e).__name__})"
    return True, f"{model} via ONNX (fastembed)"


class Reranker:
    """⚠️ LAZY, because importing this must not cost anything for the majority of searches that
    will never use it — and loading an 80 MB ONNX model on every app start to serve a feature
    nobody has switched on is exactly the kind of cost that makes a tool feel heavy."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model_name = model
        self._model = None
        self.load_seconds = 0.0
        self.last_pairs = 0
        self.last_seconds = 0.0

    def _load(self):
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            t0 = time.time()
            self._model = TextCrossEncoder(model_name=self.model_name)
            # ⚠️ TIMED, because the FIRST search after enabling this pays for the load and every
            # one after it does not. Without the number, a slow first query looks like the
            # feature being slow rather than a one-off.
            self.load_seconds = time.time() - t0
        return self._model

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        """Return a relevance score per document, aligned with the input order.

        ⚠️ ALIGNED WITH THE INPUT, NOT SORTED. Sorting here would lose the caller's ability to
        map a score back to the document it belongs to — and the mapping is the only thing that
        makes the output usable.
        """
        if not docs:
            return []
        m = self._load()
        t0 = time.time()
        try:
            scores = list(m.rerank(query, docs))
        except Exception:
            # ⚠️ A RE-RANK FAILURE MUST NOT DESTROY THE SEARCH. The first stage already produced
            # a usable list; falling back to it unchanged is strictly better than returning an
            # error for a feature the user could have done without.
            return []
        self.last_pairs = len(docs)
        self.last_seconds = time.time() - t0
        return [float(s) for s in scores]


def rerank_candidates(query: str, cands: list, snippets: dict, reranker: Reranker,
                      top_n: int = DEFAULT_TOP_N) -> dict:
    """Reorder candidates in place-ish and return a report of what changed.

    ⚠️ RETURNS THE MOVEMENT, NOT JUST THE NEW ORDER. "Re-ranked" is not a result a user can
    evaluate — "3 results moved into the top 10" is. And when nothing moves, that is worth
    knowing too: it means the first stage was already right and the six seconds bought nothing.
    """
    head = cands[:top_n]
    if not head:
        return {"moved": 0, "seconds": 0.0, "top_n": 0}

    # ⚠️ THE SNIPPET, NOT THE WHOLE DOCUMENT. The cross-encoder truncates to its own window
    # anyway, and passing 10 KB per candidate wastes most of a second serialising text the
    # model will never read. The matching snippet is also the part that actually answers the
    # query — a document is usually about many things and only one of them was searched for.
    pairs = [(snippets.get(c.doc_id) or "")[:1200] for c in head]
    scores = reranker.rerank(query, pairs)
    if not scores or len(scores) != len(head):
        return {"moved": 0, "seconds": reranker.last_seconds, "top_n": len(head),
                "error": "re-ranker returned no usable scores"}

    before = {c.doc_id: i for i, c in enumerate(head)}
    for c, s in zip(head, scores):
        c.rerank_score = s
    head.sort(key=lambda c: -c.rerank_score)
    moved = sum(1 for i, c in enumerate(head) if before[c.doc_id] != i)
    return {"moved": moved, "seconds": reranker.last_seconds, "top_n": len(head),
            "load_seconds": reranker.load_seconds,
            "worst_drop": max((before[c.doc_id] - i for i, c in enumerate(head)), default=0)}
