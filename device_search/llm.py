"""One place that talks to a model, whichever wire format it speaks.

Almost every provider imitates OpenAI's /chat/completions. Anthropic does not:

    OpenAI        POST {base}/chat/completions
                  Authorization: Bearer KEY
                  {"model":..., "messages":[{"role":"user","content":"..."}], "max_tokens":N}
                  reply.choices[0].message.content

    Anthropic     POST {base}/v1/messages
                  x-api-key: KEY
                  anthropic-version: 2023-06-01
                  {"model":..., "messages":[{"role":"user","content":"..."}], "max_tokens":N}
                  reply.content[0].text

Making the two look alike here means every caller can stay written against one
shape, and a provider that speaks a third dialect is one function rather than an
edit in five places.

WHY THIS EXISTS RATHER THAN A TRY/EXCEPT FALLBACK

A request built for the wrong endpoint fails with a 400 whose message is about a
missing field. That reads as a bad API key or a broken app, and the user has no
way to tell. Branching on the base URL is a fact we already know, not a guess.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

ANTHROPIC_VERSION = "2023-06-01"


def is_anthropic(base_url: str) -> bool:
    """Whether this endpoint speaks Anthropic's format.

    Matched on the host, not on the id, because the id is ours and a user can type
    any base URL into the "Something else" field.
    """
    return "anthropic.com" in (base_url or "").lower()


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        # Correct default on Linux; macOS needs the bundle above.
        return None


def chat(base_url: str, model: str, key: str, messages: list[dict],
         max_tokens: int = 1200, temperature: float = 0.0,
         timeout: int = 90) -> dict:
    """Send one completion. Returns {text, error, status}.

    `error` is a short human-facing string; `status` is the HTTP code when there
    was one, because 401 (bad key), 404 (bad model) and 402 (no credit) are three
    different fixes and the UI should be able to say which.
    """
    base = (base_url or "").rstrip("/")

    if is_anthropic(base):
        url = f"{base}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
    else:
        url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {key}"}
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:200]
        except Exception:
            detail = ""
        return {"text": "", "error": f"HTTP {e.code}: {detail}", "status": e.code}
    except Exception as e:
        return {"text": "", "error": f"{type(e).__name__}: {str(e)[:120]}", "status": None}

    try:
        if is_anthropic(base):
            blocks = d.get("content") or []
            text = "".join(b.get("text", "") for b in blocks
                           if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = d["choices"][0]["message"].get("content") or ""
    except Exception:
        return {"text": "", "error": "malformed response", "status": 200}

    return {"text": text.strip(), "error": "", "status": 200}


def from_env() -> dict | None:
    """The OpenAI-compatible settings from the environment, if present."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    return {
        "key": key,
        "base": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    }
