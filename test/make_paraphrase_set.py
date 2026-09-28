"""
make_paraphrase_set.py — ask an LLM for questions whose words are NOT in the answer.

===============================================================================
⚠️⚠️ WHY THIS EXISTS, AND WHY IT NEEDS A VALIDATION STEP
===============================================================================
Every measurement so far used queries built from the document's own words. That is BM25's
home turf and NOT what embeddings are for, so all three sets of numbers agreed with each
other while never testing the thing they were quoted about.

⚠️ A paraphrase query — "how do I stop the reindex starting over" when the file says "shard
resumption via content fingerprints" — is the case semantic search exists for, and it shares
no vocabulary with the answer.

⚠️ I CANNOT WRITE THOSE MYSELF. Inventing them from the corpus would make the questions depend
on my vocabulary rather than on the documents, and a query I chose because I know the answer is
a query I have unconsciously aimed at it.

⚠️ BUT AN LLM CAN, AND THAT IS WHAT IT IS FOR. Which introduces a new failure mode:

    ⚠️ AN LLM ASKED TO PARAPHRASE MAY SIMPLY NOT PARAPHRASE.

    "How do I fix the composer autoload class map?" is not a paraphrase of a document that
    says "composer autoload class map" — it is the same query with a verb attached, and it
    would measure lexical search while claiming to measure semantic search.

⚠️ SO EVERY GENERATED QUESTION IS SCORED FOR LEXICAL OVERLAP AGAINST ITS OWN DOCUMENT, AND
THE DISTRIBUTION IS REPORTED. A question sharing most of its content words with the answer is
discarded. **The set is only usable if the overlap is actually low**, and that is checked
rather than assumed — the same discipline the recall control needed after it silently
reported 0/24 on phrases known to be present.
"""

import json
import os
import subprocess
import tempfile
import random
import re
import sys
import time
import urllib.request

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from device_search.config import DB_PATH                        # noqa: E402
from device_search.store import Store                           # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "paraphrase_set.json")

# ⚠️ CONTENT WORDS, WITH THE FILLER REMOVED. Overlap measured against "the", "is" and "how"
# would be high for every question and would say nothing about whether it is a paraphrase.
STOP = set("""a an the is are was were be been being of to in on for with at by from as that this
these those it its and or but if then than so how what where when which who why do does did
can could should would will i my me you your we our they their there here not no""".split())


def load_key() -> tuple[str, str, str]:
    """Find an OpenAI-compatible endpoint.

    ⚠️ CHECKS SEVERAL PLACES because the key for this project lives in the other repo's .env —
    a sibling directory. Reading it from disk is not a convenience: it means the generator can
    be run without pasting a secret into a shell, where it would land in history.
    """
    cands = [".env", "../ai-engineer-learning/.env"]
    base = os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("OPENAI_MODEL", "")
    key = os.environ.get("OPENAI_API_KEY", "")
    for c in cands:
        if not os.path.exists(c):
            continue
        for line in open(c, encoding="utf-8"):
            line = line.strip()
            if line.startswith("OPENAI_API_KEY=") and not key:
                key = line.split("=", 1)[1].strip()
            elif line.startswith("OPENAI_BASE_URL=") and not base:
                base = line.split("=", 1)[1].strip()
            elif line.startswith("OPENAI_MODEL=") and not model:
                model = line.split("=", 1)[1].strip()
    return key, (base or "https://api.openai.com/v1"), (model or "gpt-4o-mini")


def content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOP}


def _via_curl(base, key, body) -> str:
    """⚠️⚠️ CURL IS THE TRANSPORT, AND THAT IS A DECISION RATHER THAN A SHORTCUT.

    urllib failed twice on this endpoint in ways that are not the code's fault:
    CERTIFICATE_VERIFY_FAILED (no CA bundle on macOS python.org builds) and then
    IncompleteRead(0 bytes) — the API answers with chunked transfer and the stdlib client does
    not always reassemble it.

    ⚠️ THE MANUAL ALREADY RECORDED THIS AND SOLVED IT THE SAME WAY, after the same two failures
    on the same machine. curl is present on every macOS and Linux system, handles chunking
    correctly, and returns the raw body so a failure can be READ rather than guessed at.

    ⚠️ THE KEY GOES THROUGH A FILE, NOT ARGV. `curl -H "Authorization: Bearer sk-..."` puts the
    secret in this process's command line, where `ps` shows it to every user on the machine.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(body)
        cfg = fh.name
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-m", "120", "-X", "POST",
             f"{base.rstrip('/')}/chat/completions",
             "-H", "Content-Type: application/json",
             "-H", f"Authorization: Bearer {key}",
             "--data-binary", f"@{cfg}"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            return f"__ERROR__ curl exit {proc.returncode}: {proc.stderr.strip()[:120]}"
        return proc.stdout
    finally:
        try:
            os.unlink(cfg)
        except OSError:
            pass


stats: list = []


def ask(key, base, model, phrase, context, tries=2):
    """⚠️ THE PROMPT FORBIDS THE ANSWER'S WORDS, AND THAT IS THE ENTIRE INSTRUCTION.

    Left to itself a model produces "what does X mean" where X is copied from the text. The
    explicit constraint — and the reminder that a human would not know the exact words — is
    what turns it into a paraphrase. ⚠️ It is still only a request: the overlap check in main()
    is what enforces it, because asked is not the same as obeyed.
    """
    prompt = (
        "A user is searching their own computer for a file. Below is an excerpt from the file "
        "they are looking for.\n\n"
        f"EXCERPT:\n{context}\n\n"
        "Write ONE short question the user might type (6-14 words).\n"
        "HARD RULES:\n"
        "- Use ONLY words that are NOT in the excerpt. Describe what the file DOES or CONTAINS, "
        "in your own vocabulary.\n"
        "- Do NOT quote identifiers, filenames, function names or error codes from the excerpt.\n"
        "- Do not mention the excerpt or that you were given one.\n"
        "- Output the question alone, with no preamble, quotes or explanation.\n"
    )
    # ⚠️ max_tokens IS GENEROUS ON PURPOSE. This endpoint is a REASONING model — the response
    # carries a separate `reasoning_content` field — and a tight cap produces EMPTY content with
    # finish_reason "length". The manual recorded that exact failure: a four-token cap returned
    # nothing, and a fallback silently scored the empty result as a pass.
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.0,
        # ⚠️⚠️ 3,000, NOT 400, AND THE FIRST RUN PROVED WHY.
        #
        # Every one of 60 calls came back `empty content (finish_reason=length)`: this is a
        # REASONING model, and it spends the budget on `reasoning_tokens` BEFORE it writes
        # `content`. At 400 the deliberation consumed the whole allowance and the answer was
        # never reached — the response was not an error, it was a model that ran out of room
        # mid-thought.
        #
        # ⚠️ AND THIS IS THE DOCUMENTED FAILURE FROM THE MANUAL, REPEATED HERE. A four-token cap
        # on the same endpoint returned empty content once before, and a fallback silently
        # scored the empty result as a pass. A cap that is too small does not produce an error;
        # it produces NOTHING, which is worse because it looks like a model with nothing to say.
        "max_tokens": 3000,
    })

    for attempt in range(tries):
        raw = _via_curl(base, key, body)
        if raw.startswith("__ERROR__"):
            if attempt == tries - 1:
                return raw
            time.sleep(2)
            continue
        try:
            d = json.loads(raw)
        except Exception:
            if attempt == tries - 1:
                return f"__ERROR__ unparseable: {raw[:140]}"
            time.sleep(2)
            continue
        if "error" in d:
            return f"__ERROR__ api: {str(d['error'])[:140]}"
        try:
            msg = d["choices"][0]["message"]
        except Exception:
            return f"__ERROR__ no choices: {raw[:140]}"
        # ⚠️ CONTENT FIRST, reasoning_content NEVER. A reasoning model puts its deliberation in a
        # separate field; using it would take the model's THOUGHTS for its ANSWER, and the answer
        # is the only part the prompt asked for.
        txt = (msg.get("content") or "").strip()
        if not txt:
            reason = d["choices"][0].get("finish_reason")
            # ⚠️ THE TOKEN COUNTS ARE REPORTED, NOT ASSUMED. "finish_reason=length" could mean a
            # too-small cap OR a model that will not answer; reasoning_tokens distinguishes them,
            # and without the number the diagnosis is a story rather than a measurement.
            usage = d.get("usage") or {}
            det = usage.get("completion_tokens_details") or {}
            stats.append({"reasoning_tokens": det.get("reasoning_tokens"),
                          "completion_tokens": usage.get("completion_tokens"),
                          "max_tokens": 3000, "finish": reason})
            if attempt == tries - 1:
                return f"__ERROR__ empty content (finish_reason={reason}, " \
                       f"reasoning_tokens={det.get('reasoning_tokens')})"
            continue
        txt = re.sub(r"^(question|q)\s*[:\-]\s*", "", txt, flags=re.I).strip().strip('"\'')
        return txt.split("\n")[0].strip()
    return ""


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    key, base, model = load_key()
    if not key:
        print("  no API key found (checked .env and ../ai-engineer-learning/.env)")
        print("  ⚠️ the generator works with any OpenAI-compatible endpoint; set")
        print("     OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL and re-run.")
        return 2
    print(f"  endpoint: {base}  model: {model}\n")

    store = Store(DB_PATH)
    rng = random.Random(3)

    # reuse the labelling from the recall harness so the documents are the same kind
    from measure_recall import build_cases
    pool = build_cases(store, n=n * 3)
    rng.shuffle(pool)
    print(f"  {len(pool)} candidate documents\n")

    cases, errors, rejected = [], 0, 0
    for c in pool:
        if len(cases) >= n:
            break
        ctx = c["phrase"]
        q = ask(key, base, model, c["phrase"], ctx)
        if q.startswith("__ERROR__"):
            errors += 1
            print(f"    {q}")
            continue
        if not q:
            errors += 1
            continue

        # ⚠️⚠️ THE VALIDATION. THIS IS THE PART THAT MAKES THE SET USABLE.
        doc = store.by_id(c["doc_id"])
        text = (doc[4] if doc else "") or ""
        qw = content_words(q)
        dw = content_words(text[:4000])
        if not qw:
            rejected += 1
            continue
        overlap = len(qw & dw) / len(qw)
        # ⚠️ A QUESTION SHARING MORE THAN A THIRD OF ITS CONTENT WORDS WITH THE DOCUMENT IS NOT
        # A PARAPHRASE, it is lexical search wearing a question mark. It would inflate the
        # lexical result and say nothing about the embedder.
        if overlap > 0.34:
            rejected += 1
            print(f"    ✗ overlap {overlap:.0%}  {q[:60]!r}")
            continue
        cases.append({"doc_id": c["doc_id"], "path": c["path"], "phrase": c["phrase"],
                      "query": q, "overlap": round(overlap, 3)})
        print(f"    ✓ overlap {overlap:.0%}  {q[:60]!r}")

    print()
    if errors:
        print(f"  {errors} generation error(s)")
    if rejected:
        print(f"  {rejected} rejected for sharing too much vocabulary with the answer")
    if not cases:
        print("\n  ⚠️ no usable questions were produced")
        return 1

    ov = sorted(c["overlap"] for c in cases)
    med = ov[len(ov) // 2]
    print(f"  {len(cases)} usable paraphrases")
    print(f"  lexical overlap with the answer: median {med:.0%}, max {max(ov):.0%}")
    if med > 0.25:
        print("  ⚠️ THE OVERLAP IS HIGH — these are closer to keyword queries than paraphrases,")
        print("     and any comparison built on them will favour lexical search.")
    else:
        print("  ✅ low overlap: these share little vocabulary with what they are looking for,")
        print("     which is the case embeddings exist for")

    json.dump(cases, open(OUT, "w", encoding="utf-8"), indent=1)
    print(f"\n  written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
