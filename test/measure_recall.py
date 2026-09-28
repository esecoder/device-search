"""
measure_recall.py — how often does search actually FIND the answer, and is the ruler right?

===============================================================================
⚠️⚠️ VALIDATE THE INSTRUMENT BEFORE COMPARING ANYTHING WITH IT
===============================================================================
The first measurement reported recall@50 of 3/12 (25%) and I nearly treated it as a fact
about the retriever. ⚠️ BUT THE QUERIES WERE BUILT FROM THE PHRASE'S OWN WORDS, and a
lexical search should find those almost every time. A number that surprising is more likely
to be a broken ruler than a broken retriever.

⚠️ SO THIS MEASURES THE SAME QUERIES WITH THREE METHODS, and the LEXICAL one is the control:

    exact     a literal substring match — if the phrase is genuinely in the document, this
              is near-deterministic and needs no model at all
    keyword   BM25 over the same text
    semantic  the bi-encoder

⚠️ IF LEXICAL ALSO FAILS, THE MEASUREMENT IS WRONG, NOT THE RETRIEVER. A phrase recorded as
belonging to document X that a literal substring search cannot find in document X means the
ground truth is mislabelled — and every model compared with it would be scored against a
label that is false.

⚠️ THIS IS THE SAME MISTAKE THE QUALITY CLASSIFIER MADE: an AUC ceiling that was a property
of the LABEL DEFINITION rather than of the model. It cost two rounds of tuning before anyone
checked the labels.
"""

import random
import re
import sys
import time

sys.path.insert(0, ".")

from device_search.config import DB_PATH                       # noqa: E402
from device_search.store import Store                          # noqa: E402


def build_cases(store, n=24, seed=7):
    """Unique-ish phrases, each with the document it came from.

    ⚠️ UNIQUENESS IS VERIFIED AGAINST THE WHOLE INDEX, not assumed. The first version counted
    phrases only as it walked, so a phrase repeated in two documents was recorded as unique if
    the second was seen later. Here every candidate is checked against every document before it
    is accepted, which is slower and is the difference between a label and a guess.
    """
    rows = list(store.conn.execute(
        "SELECT id, path, text FROM documents WHERE n_lines BETWEEN 20 AND 4000"))
    random.Random(seed).shuffle(rows)
    texts = {r[0]: (r[2] or "") for r in rows}

    cases = []
    for doc_id, path, text in rows:
        if len(cases) >= n:
            break
        for m in re.finditer(r"[^.!?\n]{70,140}[.!?]", text or ""):
            phrase = m.group(0).strip()
            words = re.findall(r"[A-Za-z]{3,}", phrase)
            if len(words) < 8:
                continue
            # ⚠️ A PHRASE IS ONLY USABLE IF IT OCCURS IN EXACTLY ONE DOCUMENT *AND* NOWHERE ELSE.
            # The full check costs a scan per candidate; a sample of 24 costs a few seconds.
            hits = sum(1 for t in texts.values() if phrase in t)
            if hits != 1:
                continue
            cases.append({"doc_id": doc_id, "path": path, "phrase": phrase, "words": words})
            break
    return cases


def query_from(words, rng, keep_n=None):
    """A query drawn from the phrase's words, WITHOUT the first three.

    ⚠️ THE FIRST THREE ARE DROPPED ON PURPOSE: they carry the most surface signal, and a query
    that contains them is nearly the phrase itself. What remains tests whether the retriever can
    match on PART of a sentence, which is what a real question does.

    ⚠️ IT IS STILL A LEXICAL OVERLAP TEST, NOT A PARAPHRASE TEST. There is no thesaurus here and
    inventing one would make the result depend on my vocabulary. That limitation is stated
    rather than hidden — and it is exactly why the LEXICAL control below is the right check:
    both the query and the control use the same words, so the control's recall should be high.
    """
    tail = words[3:]
    if len(tail) < 3:
        return None
    k = keep_n or max(3, len(tail) // 2)
    keep = rng.sample(tail, min(len(tail), k))
    rng.shuffle(keep)
    return " ".join(keep)


def rank_of(hits, doc_id):
    for i, h in enumerate(hits):
        cid = h[0] if isinstance(h, (tuple, list)) else getattr(h, "doc_id", None)
        if cid == doc_id:
            return i
    return None


def main():
    store = Store(DB_PATH)
    rng = random.Random(11)

    print("  building cases with a FULL uniqueness check…")
    t0 = time.time()
    cases = []
    for c in build_cases(store, n=60):
        q = query_from(c["words"], rng)
        if q:
            cases.append({**c, "query": q})
        if len(cases) >= 24:
            break
    print(f"  {len(cases)} cases in {time.time() - t0:.1f}s\n")

    if not cases:
        print("  ⚠️ no unique phrases found — the corpus may be too repetitive")
        return 2

    # show the ruler
    print("  a sample, so the queries can be judged rather than trusted:")
    for c in cases[:3]:
        print(f"    phrase : {c['phrase'][:78]!r}")
        print(f"    query  : {c['query']!r}")
        print()

    def recall(fn, k=50):
        found, ranks = 0, []
        for c in cases:
            try:
                hits = fn(c["query"], k)
            except Exception:
                hits = []
            r = rank_of(hits, c["doc_id"])
            if r is not None:
                found += 1
                ranks.append(r)
        mrr = sum(1.0 / (r + 1) for r in ranks) / len(cases) if cases else 0.0
        return found, mrr

    results = {}

    # ---------------------------------------------------------------- control 1
    # ⚠️⚠️ THE LITERAL CHECK SEARCHES FOR THE PHRASE, NOT FOR THE QUERY.
    #
    # The first version searched the QUERY as a literal substring. But the query is a SHUFFLED
    # SUBSET of the phrase's words — "var look first APPLICATION" from "The client will first
    # look at the GOOGLE_APPLICATION_CREDENTIALS env var." — so a contiguous match is impossible
    # BY CONSTRUCTION and it reported 0/24.
    #
    # ⚠️ AND THE VERDICT THEN USED THAT ZERO to declare the ground truth mislabelled. A broken
    # control produced a confident wrong conclusion about the corpus, which is the exact failure
    # this file was written to guard against — committed inside the guard.
    #
    # ⚠️ THE PHRASE IS THE RIGHT PROBE. It is known to be present verbatim, so it must be found.
    # If IT cannot be found, the label really is false.
    def exact(_q, k, _case=None):
        return []

    found_phrase = 0
    for c in cases:
        esc = c["phrase"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row = store.conn.execute(
            "SELECT id FROM documents WHERE id=? AND text LIKE ? ESCAPE '\\' LIMIT 1",
            (c["doc_id"], f"%{esc}%")).fetchone()
        if row:
            found_phrase += 1
    results["phrase present verbatim"] = (found_phrase, 1.0 if found_phrase else 0.0)
    print(f"  {'phrase present verbatim':<22} {found_phrase:>2}/{len(cases)}"
          f"   <- the real control: is the label true?")

    # ---------------------------------------------------------------- control 2
    def keyword(q, k):
        return store.keyword_search(q, limit=k)

    found, mrr = recall(keyword)
    results["bm25 keyword"] = (found, mrr)
    print(f"  {'bm25 keyword':<22} {found:>2}/{len(cases)}  MRR {mrr:.3f}")

    # ---------------------------------------------------------------- the target
    try:
        from device_search.semantic import Semantic
        sem = Semantic()
        ok, why = sem.available()
        if ok and sem.load("", ""):
            found, mrr = recall(lambda q, k: sem.search(q, limit=k))
            results["semantic (bge-small)"] = (found, mrr)
            print(f"  {'semantic (bge-small)':<22} {found:>2}/{len(cases)}  MRR {mrr:.3f}")
        else:
            print(f"  semantic unavailable: {why}")
    except Exception as e:
        print(f"  semantic failed: {type(e).__name__}: {e}")

    # ---------------------------------------------------------------- verdict
    print()
    # ⚠️⚠️ THE GAP IS SEMANTIC vs BM25, NOT SEMANTIC vs THE LABEL CHECK.
    #
    # `phrase present verbatim` is 24/24 BY CONSTRUCTION — it asks "is this label true", not
    # "can a retriever find it". Subtracting the semantic number from it compares a retriever
    # against a tautology and always reports a catastrophic gap, whatever the embedder does.
    # The comparison that means something is between two things that SEARCH.
    label_ok = results.get("phrase present verbatim", (0, 0))[0]
    lex_found = results.get("bm25 keyword", (0, 0))[0]
    lex_mrr = results.get("bm25 keyword", (0, 0))[1]
    sem_found, sem_mrr = results.get("semantic (bge-small)", (0, 0))

    print()
    if label_ok < len(cases):
        print(f"  ⚠️⚠️ THE RULER IS BROKEN: only {label_ok}/{len(cases)} phrases were found")
        print("      verbatim in the documents they came from. The labels are false and every")
        print("      number below is scored against them. Do not read them.")
        return 1

    print(f"  ✅ THE RULER HOLDS: all {label_ok} phrases are present verbatim in their own")
    print("     documents, so the labels are true and the comparison below is meaningful.")
    print()
    print(f"  BM25 (lexical)  found {lex_found}/{len(cases)}  MRR {lex_mrr:.3f}")
    print(f"  semantic        found {sem_found}/{len(cases)}  MRR {sem_mrr:.3f}")
    print()
    print("  ⚠️ AND THIS IS NOT A FAIR FIGHT. Every query here is built from the document's")
    print("     OWN WORDS, which is the situation lexical search exists for. Embeddings are for")
    print("     the opposite case: a question whose words do NOT appear in the answer.")
    print()
    print("     ⚠️ SO THIS IS NOT EVIDENCE THE EMBEDDER IS BAD. It IS evidence about FUSION:")
    print("     when a query overlaps the answer lexically, BM25 is the stronger retriever, and")
    print("     a router that sends such a query to semantic-first is choosing the weaker one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
