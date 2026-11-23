"""Does reranking help on THIS corpus? Measure, then decide.

The cross-encoder is trained on web-search passages. This corpus is source code,
markdown and PDFs. That is a real distribution shift, and the honest answer is
that a reranker may not help here - so this script measures both orderings on
queries whose correct answer is known, and prints a verdict that can be wrong.

Queries are built from the corpus: take a document, pull distinctive terms out of
it, search for those terms, and the document they came from is the relevant one.
That makes the ground truth automatic and impossible to grade generously - the
answer is defined by where the words came from, not by my judgement.

    ./.venv/bin/python test/rerank_bench.py # 40 queries
    ./.venv/bin/python test/rerank_bench.py --n 100
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_search.config import DB_PATH
from device_search.rerank import DEFAULT_TOP_N, Reranker, evaluate
from device_search.store import Store

# Words that appear in nearly every file and therefore identify none of them.
STOP = set("""
the and for with from that this have are was were will can not you your our its
def return import class self true false none if else elif while try except
const let var function new this these those then than into over under about
""".split())


def distinct_terms(text: str, k: int = 4) -> list[str]:
    """The terms in a document least likely to appear everywhere."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", text or "")
    seen, out = set(), []
    for w in words:
        lw = w.lower()
        if lw in STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= k:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="how many queries to build")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    ap.add_argument("--k", type=int, default=10, help="rank cutoff for the metrics")
    args = ap.parse_args()

    store = Store(DB_PATH)

    # Sample documents with real content. Screenshots and empty rows make poor
    # queries, so require enough text to have distinctive words.
    rows = store.conn.execute(
        "SELECT id, path, text FROM documents "
        "WHERE COALESCE(is_dir,0)=0 AND LENGTH(text) BETWEEN 400 AND 20000 "
        "ORDER BY RANDOM() LIMIT ?", (args.n * 6,)).fetchall()

    # ONLY KEEP A QUERY IF IT CAN RETRIEVE ITS OWN SOURCE.
    #
    # Measured without this filter: 6 of 12 sampled documents were not retrieved
    # at all by their own distinctive terms, and only 2 were in the top 20. The
    # sample was full of minified JavaScript, .json and generated .html, where
    # "distinctive terms" are tokens like `xvssrlni`.
    #
    # A benchmark whose ground truth is unreachable does not measure ranking - it
    # measures retrieval failure, and it will report a reranker as harmful when
    # the reranker was never given the answer to promote. The filter is not
    # leniency: it selects questions that have an answer.
    queries, relevant = [], []
    skipped = 0
    for doc_id, path, text in rows:
        terms = distinct_terms(text, 4)
        if len(terms) < 3:
            skipped += 1
            continue
        q = " ".join(terms)
        probe = store.keyword_search(q, limit=50)
        if doc_id not in {d for d, _ in probe}:
            skipped += 1
            continue
        queries.append(q)
        relevant.append({doc_id})
        if len(queries) >= args.n:
            break

    if skipped:
        print(f" skipped {skipped} sampled documents whose own terms did not retrieve them")

    if not queries:
        print(" no usable documents for a benchmark")
        return 1

    print(f" {len(queries)} queries, {args.top_n} candidates reranked, k={args.k}")
    print()

    reranker = Reranker()
    before, after = [], []
    t_retrieve = t_rerank = 0.0
    load_seconds = 0.0

    for i, q in enumerate(queries, 1):
        t0 = time.time()
        # First stage: keyword retrieval, the cheap ordering being evaluated.
        candidates = store.keyword_search(q, limit=max(args.top_n, 50))
        t_retrieve += time.time() - t0
        if not candidates:
            before.append([]); after.append([])
            continue

        ids = [doc_id for doc_id, _score in candidates]
        before.append(ids)

        # Second stage: score the head with the cross-encoder.
        head = ids[:args.top_n]
        texts = []
        for doc_id in head:
            row = store.by_id(doc_id)
            texts.append((row[4] if row else "") or "")

        t0 = time.time()
        scores = reranker.rerank(q, texts)
        t_rerank += time.time() - t0
        load_seconds = reranker.load_seconds

        if not scores or len(scores) != len(head):
            after.append(ids) # failed to score: keep the order
            continue

        order = sorted(range(len(head)), key=lambda j: -scores[j])
        after.append([head[j] for j in order] + ids[args.top_n:])

        if i % 10 == 0:
            print(f" … {i}/{len(queries)}", flush=True)

    result = evaluate(before, after, relevant, k=args.k)

    print()
    print(f" model : {reranker.model_name}")
    print(f" load (first use) : {load_seconds:.1f}s")
    print(f" retrieve total : {t_retrieve:.1f}s ({t_retrieve/len(queries)*1000:.0f} ms/query)")
    print(f" rerank total : {t_rerank:.1f}s ({t_rerank/len(queries)*1000:.0f} ms/query)")
    print()
    print(f" {'':>14}{'before':>10}{'after':>10}{'delta':>10}")
    print(f" {'MRR':>14}{result['mrr_before']:>10.4f}{result['mrr_after']:>10.4f}{result['d_mrr']:>+10.4f}")
    print(f" {'nDCG@'+str(args.k):>14}{result['ndcg_before']:>10.4f}{result['ndcg_after']:>10.4f}{result['d_ndcg']:>+10.4f}")
    print()
    print(f" verdict: {result['verdict']}")

    # The verdict is advisory and the exit code carries it, so this can gate a
    # release rather than printing a number nobody reads.
    if not result["ok"]:
        print(" reranking is NOT justified on this corpus at these settings")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
