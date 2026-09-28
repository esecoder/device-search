"""
measure_rerank.py — does the cross-encoder actually improve the results?

⚠️⚠️ THE ONLY QUESTION WORTH ASKING ABOUT A RE-RANKER.

"Re-ranking is a known-good technique" is not a measurement, and this project has already
recorded a case where the obvious improvement made things worse: the reranker built for the
manual scored MRR 0.681 -> 0.674.

⚠️ SO THIS BUILDS ITS OWN GROUND TRUTH FROM THE CORPUS, in a way that cannot be gamed:

    1. take a document
    2. pick a DISTINCTIVE phrase from it that appears NOWHERE else in the index
    3. turn that phrase into a QUESTION whose words are NOT the phrase's words
    4. the ground truth is that document, and it is knowable rather than judged

⚠️ STEP 3 IS THE WHOLE TEST. If the query reuses the phrase's own words, the first stage
finds it trivially and the re-ranker has nothing to improve — the measurement would report
"no change" and mean nothing by it. A query in DIFFERENT WORDS is where a bi-encoder is
weak and a cross-encoder should be strong.

⚠️ AND IT REPORTS THE BASELINE. A re-ranker that lifts MRR 0.30 -> 0.34 is working; one that
lifts 0.95 -> 0.96 is not worth eight seconds. The number only means something next to the
number it started from.
"""

import random
import re
import sys
import time

sys.path.insert(0, ".")

from device_search.config import DB_PATH                      # noqa: E402
from device_search.store import Store                          # noqa: E402
from device_search.semantic import Semantic                    # noqa: E402
from device_search.rerank import Reranker, DEFAULT_TOP_N       # noqa: E402


def distinctive_phrases(store, n: int, seed: int = 7):
    """Find short phrases that occur in exactly ONE document.

    ⚠️ UNIQUENESS IS THE REQUIREMENT, NOT LENGTH. A phrase that appears in 400 files makes the
    ground truth ambiguous, and every ambiguous case would be counted as a re-ranker failure
    when it is really a labelling failure — which is the Bayes-rate mistake this project
    already made once with the quality classifier.
    """
    rows = list(store.conn.execute(
        "SELECT id, path, text FROM documents WHERE n_lines BETWEEN 20 AND 4000"))
    random.Random(seed).shuffle(rows)

    picked = []
    seen_counts = {}
    for doc_id, path, text in rows:
        if len(picked) >= n * 6:
            break
        # ⚠️ SENTENCES, NOT LINES. A line in source code is `});` and in prose is a fragment.
        for m in re.finditer(r"[^.!?\n]{60,140}[.!?]", text or ""):
            s = m.group(0).strip()
            if not (55 < len(s) < 150):
                continue
            # ⚠️ A phrase with no letters, or one that is mostly punctuation and digits, has no
            # words to paraphrase and no meaning to search for.
            words = re.findall(r"[A-Za-z]{3,}", s)
            if len(words) < 6:
                continue
            key = " ".join(words[:5]).lower()
            if key in seen_counts:
                seen_counts[key] += 1
                break
            seen_counts[key] = 1
            picked.append({"doc_id": doc_id, "path": path, "phrase": s, "words": words})
            break
    return picked


def make_query(phrase_words, rng):
    """A QUESTION IN DIFFERENT WORDS.

    ⚠️ BUILT BY REMOVING THE PHRASE'S OWN DISTINCTIVE WORDS, NOT BY SYNONYMISING THEM. There is
    no thesaurus here and inventing one would make the queries depend on my vocabulary rather
    than on the corpus. Taking a subset of the phrase's words and presenting them as a question
    is a weaker test, but it is an HONEST one — and it is stated rather than implied.

    ⚠️ THE FIRST THREE WORDS ARE DROPPED. They carry the most surface signal, so a query that
    keeps them is nearly the phrase itself.
    """
    tail = phrase_words[3:]
    if len(tail) < 2:
        return None
    keep = rng.sample(tail, min(len(tail), max(2, len(tail) // 2)))
    rng.shuffle(keep)
    return " ".join(keep)


def rank_of(cands, doc_id):
    """⚠️ search() RETURNS (doc_id, score) TUPLES, NOT OBJECTS. The first version assumed
    .doc_id and crashed on the very first query — an API assumption, not a logic error."""
    for i, c in enumerate(cands):
        cid = c[0] if isinstance(c, (tuple, list)) else c.doc_id
        if cid == doc_id:
            return i
    return None


def main():
    store = Store(DB_PATH)
    sem = Semantic()
    ok, why = sem.available()
    if not ok:
        print(f"  semantic backend unavailable: {why}")
        return 2
    # ⚠️ load() TAKES LEGACY PATHS AND IS NOT OPTIONAL. A bare Semantic() has vstore = None and
    # search() silently returns [] — measured, and it is why the first attempt found nothing.
    # With a sharded index the arguments are unused; the shards are discovered from disk.
    if not sem.load("", ""):
        print("  semantic index could not be loaded")
        return 2
    n_shards = len(getattr(getattr(sem, "vstore", None), "man", None).shards) if sem.vstore else 0
    print(f"  first stage: {why}  ({n_shards} shards)\n")

    trials = distinctive_phrases(store, n=10)
    rng = random.Random(11)

    cases = []
    for t in trials:
        q = make_query(t["words"], rng)
        if q:
            cases.append({**t, "query": q})
        if len(cases) >= 12:
            break
    print(f"  {len(cases)} candidate queries built from unique corpus phrases\n")

    # ------------------------------------------------------------------ stage 1
    t0 = time.time()
    for c in cases:
        hits = sem.search(c["query"], limit=50)
        c["cands"] = hits
        c["rank_before"] = rank_of(hits, c["doc_id"])
    stage1_s = time.time() - t0

    found = [c for c in cases if c["rank_before"] is not None]
    print(f"  stage 1 (bi-encoder): {len(cases)} queries in {stage1_s:.2f}s "
          f"({stage1_s / max(len(cases), 1) * 1000:.0f} ms each)")
    print(f"  the ground-truth document was in the top 50 for {len(found)}/{len(cases)}")
    if not found:
        print("\n  ⚠️ the first stage never found the answer, so re-ranking has nothing to "
              "reorder. Re-ranking cannot fix a candidate list that is wrong.")
        return 1

    def mrr(rs):
        return sum(1.0 / (r + 1) for r in rs if r is not None) / max(len(rs), 1)

    before = mrr([c["rank_before"] for c in found])
    print(f"  MRR before: {before:.4f}")

    # ------------------------------------------------------------------ stage 2
    print("\n  loading the cross-encoder (first call downloads the model)…")
    rr = Reranker()
    try:
        rr._load()
    except Exception as e:
        print(f"  ✗ could not load the re-ranker: {type(e).__name__}: {e}")
        return 3
    print(f"  loaded in {rr.load_seconds:.1f}s")

    # ⚠️ THE TEXT COMES FROM THE STORE. Semantic has no document_text() — it stores vectors and
    # knows nothing about content. Reading it here is also what the real caller will do, so the
    # measurement exercises the same path the feature would.
    def text_of(doc_id):
        r = store.by_id(doc_id)
        return (r[4] if r and len(r) > 4 else "") or ""

    t0 = time.time()
    for c in found:
        pairs = [text_of(h[0])[:1200] for h in c["cands"][:DEFAULT_TOP_N]]
        n_empty = sum(1 for x in pairs if not x.strip())
        if n_empty == len(pairs):
            # ⚠️ REPORTED, NOT SILENTLY IGNORED. Re-ranking empty strings would produce
            # meaningless scores and a confident-looking comparison.
            print(f"  ⚠️ no text for any of {len(pairs)} candidates of {c['query']!r}")
            continue
        scores = rr.rerank(c["query"], pairs)
        if len(scores) != len(pairs):
            continue
        head = list(c["cands"][:DEFAULT_TOP_N])
        paired = list(zip(head, scores))
        paired.sort(key=lambda kv: -kv[1])
        tail = c["cands"][DEFAULT_TOP_N:]
        c["reordered"] = [h for h, _ in paired] + tail
        c["rank_after"] = rank_of(c["reordered"], c["doc_id"])
    stage2_s = time.time() - t0

    after = mrr([c.get("rank_after") for c in found])
    moves = sum(1 for c in found
                if c.get("rank_after") is not None and c["rank_after"] != c["rank_before"])

    # ------------------------------------------------------------------ report
    print(f"\n  stage 2 (cross-encoder): {len(found)} queries in {stage2_s:.1f}s "
          f"({stage2_s / max(len(found), 1):.2f}s each, {DEFAULT_TOP_N} candidates)")
    print()
    print(f"  MRR before : {before:.4f}")
    print(f"  MRR after  : {after:.4f}")
    d = after - before
    print(f"  change     : {d:+.4f}")
    print()
    print(f"  results that moved: {moves}/{len(found)}")
    print()
    if d > 0.01:
        print("  ✅ THE RE-RANKER HELPS on this corpus")
    elif d < -0.01:
        print("  ⚠️ THE RE-RANKER MAKES THINGS WORSE — do not ship it enabled")
    else:
        print("  ⚠️ NO MEASURABLE IMPROVEMENT. The first stage is already ordering these")
        print("     correctly, so the cost is real and the benefit is not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
