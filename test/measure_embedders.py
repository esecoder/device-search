"""
measure_embedders.py — which embedding model actually finds the answer?

===============================================================================
⚠️⚠️ WHY THIS IS THE RIGHT MEASUREMENT NOW, AND WHY IT WAS NOT POSSIBLE BEFORE
===============================================================================
Three previous measurements all agreed with each other and none of them tested the thing
they were quoted about: they used queries built from the document's own words, which is
lexical search's home turf. The paraphrase set fixes that — 20 questions sharing a median
of 12% of their content words with the answer.

⚠️ AND IT PRODUCED THE NUMBER THAT MATTERS: bge-small finds the right document for 7 of 20
paraphrase queries. 35%. Everything downstream — re-ranking, an LLM answer, a nicer UI —
is polishing a list that two times out of three does not contain the answer.

⚠️ SO THE MODEL IS THE BOTTLENECK, AND THIS MEASURES THE ALTERNATIVES.

===============================================================================
⚠️ THE SEARCH SPACE IS SUBSAMPLED, AND THAT IS STATED BECAUSE IT CHANGES THE NUMBER
===============================================================================
Re-embedding the full corpus for each model is ~24 minutes each. Six models is two and a
half hours, and most of it is spent proving the SAME RANKING on a smaller one.

⚠️ SO EVERY MODEL SEES THE SAME 1,600-DOCUMENT SUBSET — all 20 answer documents plus random
others. Same documents, same queries, same chunking, same metric. **The comparison between
models is fair. The absolute number is not the production number**, because a smaller search
space is an easier search space, and every model benefits equally.

⚠️ AND THE SUBSET IS IDENTICAL FOR ALL MODELS, which is the property that makes the ranking
transfer. A model that wins here was handed no advantage.
"""

import json
import random
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from device_search.config import DB_PATH                        # noqa: E402
from device_search.store import Store                           # noqa: E402
from device_search.comments import embed_target                 # noqa: E402
from measure_recall import rank_of                              # noqa: E402

SUBSET = 1600          # documents, including the 20 answers
CHUNK_CHARS = 1400     # ⚠️ the same character budget the app chunks with

# ⚠️ ORDERED BY SIZE, so the cheap models answer first and a slow download cannot hide the
# result for everything else.
MODELS = [
    "BAAI/bge-small-en-v1.5",                    # 0.07 GB — the current one, the baseline
    "sentence-transformers/all-MiniLM-L6-v2",    # 0.09 GB — the common default
    "nomic-ai/nomic-embed-text-v1.5-Q",          # 0.13 GB — quantised, 768d
    "BAAI/bge-base-en-v1.5",                     # 0.21 GB — 3x the parameters, same family
    "mixedbread-ai/mxbai-embed-large-v1",        # 0.64 GB — 1024d
    "BAAI/bge-large-en-v1.5",                    # 1.20 GB — the top of the family
]


def build_space(store, answer_ids):
    """All answer documents plus a random sample of everything else.

    ⚠️ ANSWERS FIRST, DISTRACTORS AFTER, AND THE RESULT IS SHUFFLED. Taking the answer
    documents first guarantees they are present; shuffling afterwards guarantees they are not
    clustered at low ids, which a retriever could exploit by accident.
    """
    rows = list(store.conn.execute("SELECT id, path, text FROM documents"))
    answers = [r for r in rows if r[0] in answer_ids]
    rest = [r for r in rows if r[0] not in answer_ids]
    random.Random(5).shuffle(rest)
    space = answers + rest[: max(0, SUBSET - len(answers))]
    random.Random(6).shuffle(space)
    return space


def chunks_of(path, text):
    """⚠️ THE APP'S OWN POLICY, IMPORTED RATHER THAN REIMPLEMENTED. If this used raw text it
    would measure a corpus the product never builds — and the policy is 71% of the indexing
    cost, so it changes what the models are asked to do."""
    target, _why = embed_target(path, text or "")
    if not target.strip():
        return []
    out = []
    for i in range(0, len(target), CHUNK_CHARS):
        piece = target[i:i + CHUNK_CHARS]
        if piece.strip():
            out.append(piece)
    return out


def evaluate(model_name, space, cases, dim_hint=None):
    from fastembed import TextEmbedding
    import numpy as np

    t0 = time.time()
    model = TextEmbedding(model_name)
    load_s = time.time() - t0

    # ⚠️ CHUNKS AND THEIR PARENT DOCUMENTS, kept aligned. A chunk that cannot name its document
    # cannot be scored — and collapsing to one vector per document would let a long file be
    # represented by its least relevant passage, which is not what the product does.
    texts, owners = [], []
    for doc_id, path, text in space:
        for piece in chunks_of(path, text):
            texts.append(piece)
            owners.append(doc_id)

    t0 = time.time()
    vecs = np.array(list(model.embed(texts, batch_size=32)), dtype="float32")
    embed_s = time.time() - t0
    # ⚠️ L2-NORMALISED SO THE INNER PRODUCT IS THE COSINE, which is what the app's VectorStore
    # does. Comparing unnormalised vectors would rank by length, not by meaning.
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.clip(norms, 1e-9, None)

    qvecs = np.array(list(model.embed([c["query"] for c in cases], batch_size=32)),
                     dtype="float32")
    qvecs = qvecs / np.clip(np.linalg.norm(qvecs, axis=1, keepdims=True), 1e-9, None)

    sims = qvecs @ vecs.T
    owners_arr = np.array(owners)

    found10 = found50 = 0
    ranks = []
    for i, c in enumerate(cases):
        # ⚠️ DOCUMENT-LEVEL ORDER, BEST CHUNK FIRST. Several chunks of one document collapsing
        # to one entry is what the app does, and skipping it would let a single long file
        # occupy the whole top-50 and inflate nothing but its own rank.
        order = np.argsort(-sims[i])[:300]
        seen, docs = set(), []
        for j in order:
            d = int(owners_arr[j])
            if d not in seen:
                seen.add(d)
                docs.append(d)
        r = rank_of([(d, 0.0) for d in docs], c["doc_id"])
        if r is not None:
            ranks.append(r)
            if r < 10:
                found10 += 1
            if r < 50:
                found50 += 1
    n = len(cases)
    mrr = sum(1.0 / (r + 1) for r in ranks) / n if n else 0.0
    return {"model": model_name, "dim": vecs.shape[1], "chunks": len(texts),
            "recall10": found10 / n, "recall50": found50 / n, "mrr": mrr,
            "load_s": load_s, "embed_s": embed_s,
            "per_1k_s": embed_s / max(len(texts), 1) * 1000}


def main():
    store = Store(DB_PATH)
    cases = json.load(open("test/paraphrase_set.json"))
    answer_ids = {c["doc_id"] for c in cases}
    space = build_space(store, answer_ids)
    print(f"  {len(cases)} paraphrase queries (median 12% word overlap)")
    print(f"  {len(space)} documents, all {len(answer_ids)} answers included\n")

    only = sys.argv[1:] or None
    rows = []
    for name in MODELS:
        if only and not any(o in name for o in only):
            continue
        print(f"  ── {name}")
        try:
            r = evaluate(name, space, cases)
        except Exception as e:
            print(f"     ✗ {type(e).__name__}: {str(e)[:100]}\n")
            continue
        rows.append(r)
        print(f"     dim {r['dim']}  {r['chunks']:,} chunks  "
              f"embedded in {r['embed_s']:.0f}s ({r['per_1k_s']:.0f}s per 1k)")
        print(f"     recall@10 {r['recall10']:.0%}   recall@50 {r['recall50']:.0%}   "
              f"MRR {r['mrr']:.3f}\n")

    if not rows:
        print("  no model produced a result")
        return 1

    print("  ═" * 36)
    print(f"  {'model':<44}{'rec@10':>7}{'rec@50':>8}{'MRR':>7}{'s/1k':>7}")
    for r in sorted(rows, key=lambda r: -r["mrr"]):
        print(f"  {r['model'].split('/')[-1]:<44}{r['recall10']:>6.0%}"
              f"{r['recall50']:>8.0%}{r['mrr']:>7.3f}{r['per_1k_s']:>7.0f}")

    best = max(rows, key=lambda r: r["mrr"])
    base = next((r for r in rows if "bge-small" in r["model"]), None)
    print()
    if base and best["model"] != base["model"]:
        gain = best["mrr"] - base["mrr"]
        cost = best["per_1k_s"] / max(base["per_1k_s"], 1)
        print(f"  ✅ {best['model'].split('/')[-1]} beats the current model by {gain:+.3f} MRR")
        print(f"     ⚠️ at {cost:.1f}x the embedding cost per chunk")
        print(f"     ⚠️ on a {SUBSET}-document subset — the production corpus is larger, so")
        print(f"        these absolute numbers are optimistic for every model equally")
    else:
        print("  ⚠️ nothing beats the current model — the bottleneck is not its size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
