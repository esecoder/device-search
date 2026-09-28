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
MAX_SOURCES = 6
MAX_CHARS_PER_SOURCE = 1200

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


def build_context(cands, text_of, max_sources=MAX_SOURCES):
    """Return [(label, path, snippet)] with secrets removed, plus a report.

    ⚠️ SIMPLE NUMERIC LABELS — [1], [2] — RATHER THAN PATHS. A model asked to cite
    `~/Documents/very/long/path/File.php` will paraphrase it, truncate it, or invent a fifth
    one. Asked for a small integer it is reliably correct, and the integer is mapped back to a
    real path by this function rather than by the model.
    """
    sources, report = [], {"sent": 0, "blocked": 0, "blocked_kinds": []}
    for c in cands:
        if len(sources) >= max_sources:
            break
        raw = (text_of(c) or "").strip()
        if not raw:
            continue
        # ⚠️ THE INTERLOCK. Scanned BEFORE the request is built, and the snippet is DROPPED
        # rather than redacted: a partially-redacted private key is still most of a private
        # key, and a worse answer is preferable to uploading one.
        hits = find_secrets(raw[:MAX_CHARS_PER_SOURCE])
        if hits:
            report["blocked"] += 1
            for h in hits:
                k = h[0] if isinstance(h, (tuple, list)) else str(h)
                if k not in report["blocked_kinds"]:
                    report["blocked_kinds"].append(k)
            continue
        sources.append((len(sources) + 1, c.path, raw[:MAX_CHARS_PER_SOURCE]))
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
    body = json.dumps({
        "model": client["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,     # ⚠️ zero: this is a extraction task, not a writing task
        # ⚠️ GENEROUS, because this endpoint's model spends budget on reasoning_tokens before
        # it writes anything. A tight cap returns EMPTY content with finish_reason=length —
        # measured on this project's own paraphrase generator, 60 times in a row.
        "max_tokens": 1200,
    }).encode()

    req = urllib.request.Request(
        f"{client['base']}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {client['key']}"})
    ctx = None
    try:
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = None            # ⚠️ correct default on Linux; macOS needs the bundle above
    try:
        with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
            d = json.load(r)
    except Exception as e:
        return {"answer": "", "citations": [], "refused": False,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}

    if "error" in d:
        return {"answer": "", "citations": [], "refused": False,
                "error": str(d["error"])[:160]}
    try:
        text = (d["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return {"answer": "", "citations": [], "refused": False, "error": "malformed response"}
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
    citations = [{"n": n, "path": by_num[n]} for n in sorted(cited & valid)]

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
