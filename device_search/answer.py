"""
answer.py — turn retrieved results into an answer, with the citations kept.

===============================================================================
⚠️⚠️ THE HALF THAT DID NOT EXIST
===============================================================================
`llm_rerank` reorders results using a model. Nothing ever SYNTHESISED an answer from
them — so the project had retrieval and re-ranking and no generation, which is the part a
user recognises as "asking a question".

⚠️ AND IT IS THE PART THAT NEEDS THE MOST RESTRAINT, NOT THE MOST CAPABILITY.

    retrieval   returns a list the user can open and check
    answering   returns a SENTENCE, and a sentence is believed

⚠️ A search tool that says "no results" is honestly empty. One that says "the retry logic is
in RetryMiddleware.php" when nothing in the results says that is a tool that has started
inventing things about the user's own files — and they have no way to tell, because it looks
exactly like the correct answers.

⚠️ SO THREE RULES ARE ENFORCED HERE RATHER THAN REQUESTED IN A PROMPT:

  1. EVERY CLAIM CARRIES A CITATION. The model is asked to tag each sentence with the source
     it came from, and a sentence whose tag names a source that was not provided is DROPPED.
     Asking for citations is not the same as them being true.

  2. IT REFUSES WHEN THE CONTEXT IS THIN. If the retrieved snippets do not contain the
     answer, the correct output is that sentence, not a plausible paragraph. A model asked an
     unanswerable question from context will usually still answer it from its weights.

  3. THE SAME SECRET INTERLOCK AS RE-RANKING. This sends snippets to an API, and a search
     index over `~` contains SSH keys. Anything matching a secret pattern is dropped before
     the request is built, and the drop is reported.

⚠️ AND THE ANSWER IS ALWAYS SHOWN WITH ITS SOURCES, so the user can check it. An answer
without its evidence is a claim.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from .config import find_secrets

# ⚠️ HOW MUCH CONTEXT IS SENT. This is a device search answering a question about a file, not
# a report. Six documents at 1,200 characters is ~2,000 tokens — enough to answer, small
# enough that the model attends to all of it rather than losing the middle, and cheap.
MAX_SOURCES = 8

# A TOKEN BUDGET, NOT A CHARACTER BUDGET.
#
# The first version capped each source at 1,200 characters and sent up to six of
# them. Characters do not bound tokens: the measured ratio on this corpus is about
# 3.4 characters per token, so six sources was 2,100 tokens on average and could be
# far more on dense text - code and tables run nearer 3.0, which is 2,400 tokens
# from the same six snippets. A limit that cannot predict the thing it is limiting
# is not a limit.
#
# 6,000 tokens leaves room for the question and the answer inside a 8k-16k window
# without crowding out either.
CONTEXT_TOKEN_BUDGET = 6_000

# No single snippet may take more than this share of the budget.
#
# Measured: with only the total budget enforced, one 20,000-character document
# consumed 5,883 of 6,000 tokens and the other nine files were never sent. The
# answer then reflected whichever file happened to be retrieved first, which is
# the opposite of context engineering. A per-snippet ceiling makes the budget
# shared rather than winner-takes-all.
MAX_TOKENS_PER_SOURCE = 1_200

# Characters per token. Measured on this corpus (see semantic.py, where the same
# ratio drove the chunk size). Used only when the embedder's tokenizer is
# unavailable, and kept as a single constant so the two cannot drift.
CHARS_PER_TOKEN = 3.4


def estimate_tokens(text: str) -> int:
    """Approximate the token count of a string.

    The embedder's tokenizer is used when it is already loaded, because it is
    exact. Building one purely to measure would cost more than the estimate is
    worth, so the fallback is the measured ratio.
    """
    if not text:
        return 0
    try:
        from .semantic import _TOKENIZER      # set once the model has loaded
        if _TOKENIZER is not None:
            return len(_TOKENIZER.encode(text, add_special_tokens=False))
    except Exception:
        pass
    return int(len(text) / CHARS_PER_TOKEN) + 1

# ⚠️ THE REFUSAL SENTENCE IS DEFINED HERE AND CHECKED BELOW, so the set of things that count as
# "no answer" is explicit rather than whatever the model happens to phrase.
REFUSAL = "I could not find that in your files."


def _client():
    """⚠️ The key comes from the environment. Identical to agent.py's helper on purpose — one
    convention for how this project reaches an API, not two that drift apart."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return {"key": key, "base": base, "model": model}


PAGE_MARKER = re.compile(r"\[page (\d+)\]")


def page_span(snippet: str) -> tuple[int, int] | None:
    """The pages a snippet covers, from the markers pdftext leaves in the text.

    Returns (first, last) or None when the snippet has no markers - which is the
    normal case for anything that is not a PDF.

    A snippet can straddle a page break, so the answer is a range rather than a
    single number. Reporting only the first page of a two-page quote would send
    the reader to the wrong place half the time.
    """
    nums = [int(m) for m in PAGE_MARKER.findall(snippet or "")]
    if not nums:
        return None
    return min(nums), max(nums)


def _signature(text: str, n: int = 240) -> str:
    """A normalised prefix, used to spot the same content arriving twice.

    Retrieval returns overlapping chunks and, when the query matches several
    places in one file, the same passage can be sent more than once. Paying
    tokens for a duplicate costs accuracy: it makes one passage look like
    corroboration when it is repetition.
    """
    head = re.sub(r"\s+", " ", (text or "")[:n]).strip().lower()
    return head


def build_context(cands, text_of, max_sources=MAX_SOURCES,
                  token_budget=CONTEXT_TOKEN_BUDGET,
                  max_per_document=2):
    """Return [(label, path, snippet)] with secrets removed, plus a report.

    Three rules, in this order, because each one changes what fits:

      1. DROP DUPLICATES. Two snippets with the same opening are one source, not
         two. Sending both makes a repeated claim look corroborated.

      2. LIMIT EACH FILE. Two passages from one document is enough to establish
         what it says. More than that crowds out other files, which is how an
         answer ends up reflecting whichever document happened to be longest.

      3. SPEND A TOKEN BUDGET, not a character count. Each snippet is trimmed to
         what remains, and the loop stops when the budget is gone.

    Labels are small integers rather than paths: a model asked to cite
    `~/Documents/very/long/path/File.php` paraphrases, truncates, or invents a
    fifth one. Asked for an integer it is reliably correct, and the mapping back
    to a real path happens here rather than in the model.
    """
    sources, report = [], {
        "sent": 0, "blocked": 0, "blocked_kinds": [],
        "dropped_duplicate": 0, "dropped_over_quota": 0,
        "tokens_used": 0, "budget": token_budget, "truncated": 0,
    }
    seen: set[str] = set()
    per_doc: dict[str, int] = {}

    for c in cands:
        if len(sources) >= max_sources:
            break
        raw = (text_of(c) or "").strip()
        if not raw:
            continue

        sig = _signature(raw)
        if sig and sig in seen:
            report["dropped_duplicate"] += 1
            continue

        if per_doc.get(c.path, 0) >= max_per_document:
            report["dropped_over_quota"] += 1
            continue

        remaining = token_budget - report["tokens_used"]
        if remaining <= 50:
            break

        # The smaller of what is left and what one source is allowed. Without the
        # second term the first large document takes the whole budget.
        allowance = min(remaining, MAX_TOKENS_PER_SOURCE)

        # Trim before the secret scan, so the scan sees exactly what would be sent.
        max_chars = int(allowance * CHARS_PER_TOKEN)
        snippet = raw[:max_chars]
        if len(raw) > len(snippet):
            report["truncated"] += 1

        # THE INTERLOCK. The snippet is DROPPED, not redacted: a partially
        # redacted private key is still most of a private key.
        hits = find_secrets(snippet)
        if hits:
            report["blocked"] += 1
            for h in hits:
                k = h[0] if isinstance(h, (tuple, list)) else str(h)
                if k not in report["blocked_kinds"]:
                    report["blocked_kinds"].append(k)
            continue

        sources.append((len(sources) + 1, c.path, snippet))
        seen.add(sig)
        per_doc[c.path] = per_doc.get(c.path, 0) + 1
        report["tokens_used"] += estimate_tokens(snippet)
        report["sent"] += 1

    return sources, report


def ask(query: str, sources, client=None) -> dict:
    """Generate an answer from the sources. Returns {answer, citations, refused, error}."""
    client = client or _client()
    if not client:
        return {"answer": "", "citations": [], "refused": False,
                "error": "no model configured (set OPENAI_API_KEY)"}
    if not sources:
        return {"answer": REFUSAL, "citations": [], "refused": True, "error": ""}

    block = "\n\n".join(f"[{n}] {path}\n{snippet}" for n, path, snippet in sources)
    prompt = (
        "You are answering a question about files on the user's own computer.\n"
        "Use ONLY the numbered sources below. They are the complete context.\n\n"
        f"QUESTION: {query}\n\n"
        f"SOURCES:\n{block}\n\n"
        "RULES:\n"
        "- Answer in 1-3 sentences.\n"
        "- End EVERY sentence with the bracketed number of the source it came from, like [1].\n"
        "- If the sources do not contain the answer, reply with exactly: "
        f'"{REFUSAL}"\n'
        "- Do not use any knowledge of your own. The sources are the only facts available.\n"
        "- Do not describe the sources or mention these rules. Just answer.\n"
    )
    # One helper, two wire formats: see device_search/llm.py. Anthropic is not
    # OpenAI-compatible - it uses /v1/messages and x-api-key - and a request built
    # for the wrong endpoint fails with a 400 about a missing field, which reads as
    # a bad key rather than a wrong shape.
    from .llm import chat
    res = chat(client["base"], client["model"], client["key"],
               [{"role": "user", "content": prompt}],
               max_tokens=1200, temperature=0.0)
    if res["error"]:
        return {"answer": "", "citations": [], "refused": False,
                "error": res["error"][:160]}
    text = res["text"]

    if not text:
        return {"answer": "", "citations": [], "refused": False,
                "error": "the model returned no content"}

    # ⚠️⚠️ THE CITATION CHECK, AND IT IS THE POINT OF THIS MODULE.
    #
    # A model asked to cite will sometimes cite a source that does not exist — [7] when six
    # were provided — and that is the failure that makes an answer untrustworthy, because the
    # sentence looks sourced. Any cited number outside the range of what was sent is treated
    # as an uncited claim, and uncited claims are REPORTED rather than silently kept.
    valid = {n for n, _p, _s in sources}
    cited = {int(m) for m in re.findall(r"\[(\d+)\]", text)}
    unknown = sorted(cited - valid)
    by_num = {n: p for n, p, _s in sources}
    pages = {}
    for n, _p, snip in sources:
        span = page_span(snip)
        if span:
            pages[n] = span
    citations = []
    for n in sorted(cited & valid):
        c = {"n": n, "path": by_num[n]}
        if n in pages:
            c["page_first"], c["page_last"] = pages[n]
        citations.append(c)

    refused = text.strip().strip('"').startswith(REFUSAL[:30])
    out = {"answer": text, "citations": citations, "refused": refused, "error": ""}
    if unknown:
        # ⚠️ THE WHOLE ANSWER IS FLAGGED, NOT JUST THE OFFENDING SENTENCE. A sentence that
        # cites a source that does not exist is not a citation error; it is evidence the model
        # is not grounding its output in what it was given.
        out["invented_citations"] = unknown
    if not citations and not refused:
        out["uncited"] = True      # ⚠️ an answer with no sources is an assertion, not a result
    return out
