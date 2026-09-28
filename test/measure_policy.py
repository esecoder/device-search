"""
measure_policy.py — what should actually be embedded, now that the model is ruled out.

===============================================================================
⚠️⚠️ THE MEASUREMENT THAT RULES OUT THE MODEL LEAVES ONE BIG CANDIDATE
===============================================================================
Three models were compared over the same corpus and the same queries. bge-small (0.07 GB),
MiniLM (0.09 GB) and bge-base (0.21 GB, 3x the parameters) produced recall@10 of 25%, 15%
and 25%. **A 3x bigger model found not one additional answer.**

⚠️ SO THE PROBLEM IS NOT THE EMBEDDER. It is what the embedder is given.

    answers whose passage IS in the embedded text : 17/20
    answers that were NEVER INDEXED               :  3/20

Of the three unanswerable ones: `installed.json` embeds 0% of itself under the structural
policy, and two PHP files embed only 17% and 19% — because the policy embeds CODE COMMENTS
ONLY, and their answers are in the CODE.

⚠️ RECALL IS CAPPED AT 85% BY THE POLICY ITSELF. Measured recall is 35%. The gap between
those two numbers is what this file exists to close.

===============================================================================
⚠️ THE THREE POLICIES, AND WHY EACH IS PLAUSIBLE
===============================================================================
    comments   what the app does now — the prose inside code, nothing else
    raw        the file exactly as it is — code, comments, punctuation, all of it
    both       two passes over each file, one per form, fused

⚠️ EACH HAS A REAL ARGUMENT FOR IT:

  - COMMENTS assume a file's description lives in its comments. True when comments are
    written, which is 94% of the time in this corpus — and useless when they are not, which
    is exactly the two files above.

  - RAW assumes a general model can read code. This is the one most people expect to be bad,
    and ⚠️ IT MAY BE RIGHT: a bi-encoder trained on prose sees `$this->request->getParam()`
    as a string of near-meaningless tokens.

  - BOTH is the expensive hedge, and the argument against it is real: it doubles the index
    and adds a second, noisier list to every query — which is precisely the mistake the
    fusion measurement caught when semantic's top-40 was diluting BM25.

⚠️ THE RESULT DECIDES WHICH ARGUMENT IS TRUE. Not the reasoning.
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
from device_search.codegraph import strip_comments_and_strings, lang_for   # noqa: E402
from measure_recall import rank_of                              # noqa: E402

SUBSET = 1600
CHUNK_CHARS = 1400
MODEL = "BAAI/bge-small-en-v1.5"


def split_identifiers(text: str) -> str:
    """⚠️ `retryWithBackoff` -> `retry with backoff`, `MAX_FILE_BYTES` -> `MAX FILE BYTES`.

    A general-purpose bi-encoder was trained on prose. In prose, a word is separated by
    spaces; in code it is not, and `retryWithBackoff` arrives as a single token the model has
    almost certainly never seen — so it contributes nothing, or contributes noise.

    ⚠️ SPLITTING DOES NOT CHANGE WHAT THE CODE MEANS, only how it is spelled for the model.
    It is the cheapest possible bridge between prose and code, and it costs nothing to try.
    """
    out = []
    for tok in text.split():
        if len(tok) < 6 or not any(c.isalpha() for c in tok):
            out.append(tok)
            continue
        # camelCase and PascalCase
        s = __import__("re").sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tok)
        # snake_case and dotted/namespaced paths
        s = __import__("re").sub(r"[_\.\\/]+", " ", s)
        out.append(s)
    return " ".join(out)


def variants(path, text, policy):
    text = text or ""
    if policy == "comments":
        target, _ = embed_target(path, text)
        return [target] if target.strip() else []

    if policy == "raw":
        # ⚠️ COMMENTS AND STRINGS KEPT, KEYWORDS AND PUNCTUATION KEPT — the file as written.
        # Stripping them would be a different experiment, and a worse one: comments are the
        # part most likely to match a question.
        return [text] if text.strip() else []

    if policy == "raw_split":
        return [split_identifiers(text)] if text.strip() else []

    # ⚠️⚠️ THE ADAPTIVE POLICY, PROPOSED BY THE USER AND SUPPORTED BY THE DATA.
    #
    # Measured over 4,395 code files: 600 embed 0% of themselves and a further 972 embed
    # under 10%. A third of the corpus is invisible to meaning-based search, and the files
    # that suffer most are machine-generated ones that have no comments by nature.
    #
    # ⚠️ BUT `both` — raw code everywhere — gains recall@10 (25% -> 35%) AND LOSES MRR
    # (0.139 -> 0.100), at 4.4x the index. Adding 12,000 raw chunks dilutes the ranking, the
    # same way semantic's top-40 did.
    #
    # ⚠️ SO THE QUESTION THIS ANSWERS IS WHERE THE MRR LOSS COMES FROM: the VOLUME of raw
    # code, or something about raw code itself. Applying it only where comments are thin
    # separates the two — and if the loss was volume, this keeps the recall gain for a
    # fraction of the cost.
    if policy == "adaptive" or policy.startswith("adaptive:"):
        # ⚠️ THE THRESHOLD IS IN ABSOLUTE CHARACTERS, NOT A PERCENTAGE. A 40 KB file with 10%
        # comments yields 4 KB of prose, which is a description. A 400-byte file with 30%
        # yields 120 bytes, which is not. Percentage alone would send the large, well-commented
        # file to raw code and the tiny one to comments.
        thresh = int(policy.split(":")[1]) if ":" in policy else 600
        comment_target, _ = embed_target(path, text)
        if len(comment_target.strip()) >= thresh:
            return [comment_target]
        return [text] if text.strip() else []

    if policy == "both":
        comment_target, _ = embed_target(path, text)
        out = [t for t in (comment_target, text) if t and t.strip()]
        return out

    if policy == "both_split":
        comment_target, _ = embed_target(path, text)
        out = [t for t in (comment_target, text) if t and t.strip()]
        return [t if i == 0 else split_identifiers(t) for i, t in enumerate(out)]

    raise ValueError(policy)


def chunks_of(path, text, policy):
    out = []
    for v in variants(path, text, policy):
        for i in range(0, len(v), CHUNK_CHARS):
            piece = v[i:i + CHUNK_CHARS]
            # ⚠️ A CHUNK THAT IS ONLY PUNCTUATION IS NOT A CHUNK. Raw code produces many of
            # them (closing braces, array commas) and embedding them spends time to add
            # near-identical vectors that compete on equal terms in the fusion.
            if len(piece.strip()) > 40:
                out.append(piece)
    return out


def evaluate(policy, space, cases, model_name=MODEL):
    from fastembed import TextEmbedding
    import numpy as np

    model = TextEmbedding(model_name)
    texts, owners = [], []
    for doc_id, path, text in space:
        for piece in chunks_of(path, text, policy):
            texts.append(piece)
            owners.append(doc_id)

    t0 = time.time()
    vecs = np.array(list(model.embed(texts, batch_size=32)), dtype="float32")
    embed_s = time.time() - t0
    vecs /= np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9, None)

    qvecs = np.array(list(model.embed([c["query"] for c in cases], batch_size=32)),
                     dtype="float32")
    qvecs /= np.clip(np.linalg.norm(qvecs, axis=1, keepdims=True), 1e-9, None)

    sims = qvecs @ vecs.T
    owners_arr = np.array(owners)
    f10 = f50 = 0
    ranks = []
    for i, c in enumerate(cases):
        order = np.argsort(-sims[i])[:400]
        seen, docs = set(), []
        for j in order:
            d = int(owners_arr[j])
            if d not in seen:
                seen.add(d)
                docs.append(d)
        r = rank_of([(d, 0.0) for d in docs], c["doc_id"])
        if r is not None:
            ranks.append(r)
            f10 += r < 10
            f50 += r < 50
    n = len(cases)
    return {"policy": policy, "chunks": len(texts), "chars": sum(len(t) for t in texts),
            "recall10": f10 / n, "recall50": f50 / n,
            "mrr": sum(1.0 / (r + 1) for r in ranks) / n, "embed_s": embed_s}


def main():
    store = Store(DB_PATH)
    cases = json.load(open("test/paraphrase_set.json"))
    answer_ids = {c["doc_id"] for c in cases}
    rows = list(store.conn.execute("SELECT id, path, text FROM documents"))
    answers = [r for r in rows if r[0] in answer_ids]
    rest = [r for r in rows if r[0] not in answer_ids]
    random.Random(5).shuffle(rest)
    space = answers + rest[: SUBSET - len(answers)]
    random.Random(6).shuffle(space)

    print(f"  {len(cases)} paraphrase queries, {len(space)} documents")
    print(f"  ⚠️ 17 of the 20 answers are embedded under SOME policy; 3 are not")
    print(f"     under the current one. recall cannot exceed 85% however good the model.\n")

    policies = sys.argv[1:] or ["comments", "raw", "raw_split", "both"]
    results = []
    for pol in policies:
        print(f"  ── {pol}")
        try:
            r = evaluate(pol, space, cases)
        except Exception as e:
            print(f"     ✗ {type(e).__name__}: {str(e)[:100]}\n")
            continue
        results.append(r)
        print(f"     {r['chunks']:,} chunks  {r['chars']/1e6:.1f} MB of text  "
              f"({r['embed_s']:.0f}s)")
        print(f"     recall@10 {r['recall10']:.0%}   recall@50 {r['recall50']:.0%}   "
              f"MRR {r['mrr']:.3f}\n")

    if not results:
        return 1
    print("  ═" * 34)
    print(f"  {'policy':<16}{'chunks':>9}{'rec@10':>8}{'rec@50':>8}{'MRR':>8}{'s':>6}")
    for r in sorted(results, key=lambda r: -r["mrr"]):
        print(f"  {r['policy']:<16}{r['chunks']:>9,}{r['recall10']:>7.0%}"
              f"{r['recall50']:>8.0%}{r['mrr']:>8.3f}{r['embed_s']:>6.0f}")

    best = max(results, key=lambda r: r["mrr"])
    base = next((r for r in results if r["policy"] == "comments"), None)
    print()
    if base and best["policy"] != "comments":
        print(f"  ✅ {best['policy']} beats comments-only by "
              f"{best['mrr'] - base['mrr']:+.3f} MRR")
        print(f"     at {best['chunks'] / max(base['chunks'], 1):.1f}x the index size")
    else:
        print("  ⚠️ comments-only wins — the policy is not the problem either")
    return 0


if __name__ == "__main__":
    sys.exit(main())
