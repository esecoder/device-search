"""
measure_fusion.py — does the semantic backend HELP or HURT, once it is fused with BM25?

===============================================================================
⚠️⚠️ THE FIX I PROPOSED WOULD HAVE DONE NOTHING, AND CHECKING FIRST IS THE POINT
===============================================================================
I said the router should send lexical-overlap queries to BM25 first instead of semantic
first. ⚠️ THAT IS A NO-OP. Reciprocal Rank Fusion sums a document's contributions across
lists:

    score(d) = sum over backends of 1 / (k + rank_of_d_in_that_backend)

The ORDER OF THE LISTS DOES NOT ENTER THE ARITHMETIC. Swapping ["semantic", "keyword"] for
["keyword", "semantic"] produces byte-identical output, and I would have shipped it, measured
no change, and had no idea why.

⚠️ SO THE QUESTION IS NOT WHICH BACKEND RUNS FIRST. It is whether a WEAK backend should be in
the fusion at all. Semantic found 10/24 on these queries while BM25 found 21/24 — so its
results are mostly noise, and RRF gives every one of them the SAME WEIGHT as a BM25 hit.

⚠️ AND THIS MEASURES THE ACTUAL HARM, in three configurations, over the same cases:

    bm25 only        the baseline
    fused (current)  what the app does today
    weighted         what a fix would look like

A fix that is not preceded by a measurement of the problem is a guess with a commit message.
"""

import random
import re
import sys
import time

sys.path.insert(0, ".")

from device_search.config import DB_PATH                        # noqa: E402
from device_search.store import Store                           # noqa: E402
from device_search.agent import rrf                             # noqa: E402

# ⚠️ imported from the test that already builds them, so the cases cannot drift apart between
# two measurements and flatter whichever one is run second.
sys.path.insert(0, "test")
from measure_recall import build_cases, query_from, rank_of     # noqa: E402


def main():
    store = Store(DB_PATH)
    rng = random.Random(11)

    cases = []
    for c in build_cases(store, n=80):
        q = query_from(c["words"], rng)
        if q:
            cases.append({**c, "query": q})
        if len(cases) >= 24:
            break
    print(f"  {len(cases)} cases\n")

    from device_search.semantic import Semantic
    sem = Semantic()
    ok, why = sem.available()
    if not ok or not sem.load("", ""):
        print(f"  semantic unavailable: {why}")
        return 2
    print(f"  backends: bm25 + {why}\n")

    def measure(name, fn):
        found, ranks = 0, []
        for c in cases:
            hits = fn(c["query"])
            r = rank_of(hits, c["doc_id"])
            if r is not None:
                found += 1
                ranks.append(r)
        mrr = sum(1.0 / (r + 1) for r in ranks) / len(cases) if cases else 0.0
        print(f"  {name:<34} {found:>2}/{len(cases)}   MRR {mrr:.3f}")
        return found, mrr

    def bm25_only(q):
        return store.keyword_search(q, limit=50)

    def fused_current(q):
        lists = {"keyword": store.keyword_search(q, limit=40),
                 "semantic": sem.search(q, limit=40)}
        return rrf(lists)[:50]

    def fused_no_semantic(q):
        # ⚠️ the same call the app makes, minus the noisy backend
        lists = {"keyword": store.keyword_search(q, limit=40)}
        return rrf(lists)[:50]

    def fused_semantic_capped(q):
        """⚠️ SEMANTIC CONTRIBUTES ONLY ITS HEAD, NOT ITS TAIL.

        A bi-encoder's top-1 is meaningfully more likely to be right than its top-40, and on
        this corpus the tail is dominated by narrow-cone similarity rather than relevance. So
        the semantic list is truncated hard, which keeps whatever it genuinely adds while
        stopping forty near-random documents from competing on equal terms with BM25's hits.
        """
        sem_hits = sem.search(q, limit=40)[:5]
        lists = {"keyword": store.keyword_search(q, limit=40), "semantic": sem_hits}
        return rrf(lists)[:50]

    print("  ── the configurations ──")
    b_found, b_mrr = measure("bm25 only (baseline)", bm25_only)
    c_found, c_mrr = measure("fused, semantic top-40 (current)", fused_current)
    measure("fused, keyword only (control)", fused_no_semantic)
    s_found, s_mrr = measure("fused, semantic top-5", fused_semantic_capped)

    print()
    print("  ── what it means ──")
    d_found, d_mrr = c_found - b_found, c_mrr - b_mrr
    print(f"  semantic's effect on the fused result: {d_found:+d} found, {d_mrr:+.3f} MRR")
    if d_found < 0 or d_mrr < -0.01:
        print("  ⚠️ SEMANTIC IS HURTING. It finds fewer answers than BM25 and RRF gives its")
        print("     results equal weight, so they dilute the ones that are right.")
    elif abs(d_mrr) < 0.01:
        print("  ⚠️ SEMANTIC IS DOING NOTHING MEASURABLE on this query type — it neither")
        print("     helps nor hurts, and it costs 33 ms and an 80 MB model per query.")
    else:
        print("  ✅ semantic is adding answers BM25 misses — keep it")

    d2_found, d2_mrr = s_found - c_found, s_mrr - c_mrr
    print()
    print(f"  capping semantic at top-5:            {d2_found:+d} found, {d2_mrr:+.3f} MRR")
    if d2_mrr > 0.01:
        print("  ✅ CAPPING HELPS — the semantic tail was noise, the head is signal")
    else:
        print("  capping does not help measurably; the problem is not the tail length")
    return 0


if __name__ == "__main__":
    sys.exit(main())
