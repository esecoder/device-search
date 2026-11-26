#!/usr/bin/env python3
"""
server.py — the local API the desktop UI talks to.


WHY A DAEMON AND NOT A SUBPROCESS PER QUERY

Measured on this machine:

    python start + open the index : 0.051s
    embedding backend ready : 0.338s
    query encode, warm : 0.064s

A search box that spawns a process per keystroke pays ~0.4s EVERY TIME. A warm daemon pays it
once. **0.064s feels instant; 0.4s feels broken** — and the same numbers decide why the model
is loaded at startup rather than lazily on first query.


THE SECURITY PROBLEM NOBODY MENTIONS ABOUT LOCAL SERVERS

This endpoint returns THE CONTENTS OF THE USER'S FILES. Binding to 127.0.0.1 is NOT enough:

    * ANY process on the machine can reach a localhost port — including a random npm package
    * ANY web page the user visits can issue requests to http://127.0.0.1:PORT. The browser
      blocks READING the response without CORS, but the request still happens, and a
      **DNS-rebinding** attack makes the browser believe the attacker's domain IS localhost,
      which defeats CORS entirely and lets them read everything.

So three defences, and each one is load-bearing:

    1. BIND 127.0.0.1 ONLY — never 0.0.0.0. This is necessary and not sufficient.
    2. A TOKEN, written 0600 — every request must carry it. A random web page cannot read
                                 the token file, so it cannot use the API even if it can reach it.
    3. HOST HEADER CHECK — reject anything whose Host is not localhost/127.0.0.1. This is
                                 what actually stops DNS rebinding, because the rebinding trick
                                 depends on sending the attacker's hostname.

AND THE TOKEN IS NOT IN THE URL. Query strings land in logs and shell history.
"""

from __future__ import annotations

import json
import os
import shutil
import secrets
import socket
import subprocess
import urllib.error
import urllib.request
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import AUDIO_EXTS, DB_PATH, INDEX_DIR, VEC_PATH, VIDEO_EXTS

# Formats the image index covers. Kept here rather than in config because nothing
# else needs it: it exists so coverage() can say "1,210 of 12,554 pictures" instead
# of guessing from a count of OCR documents, which is a different set.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"}
from .store import Store

IDS_PATH = INDEX_DIR / "vectors.ids.npy"
TOKEN_PATH = INDEX_DIR / "api.token"
DEFAULT_PORT = 8734

# AN ALLOWLIST, NOT A WILDCARD. These are the origins the webview can actually have:
# tauri://localhost macOS and Linux, Tauri's custom scheme
# http://tauri.localhost Windows
# http://127.0.0.1:<port> a browser pointed directly at the daemon, for debugging
# A wildcard would let any web page the user visits read their indexed file contents.
# THE POLICY NUMBERS, stated here so they are arguable rather than buried:
# 300s how often to LOOK for drift. Cheap — a manifest read and a COUNT(*) .
# 900s how often to ACT. A repair is minutes of CPU, so acting on every glance
# at the disk would keep a working machine permanently busy.
REPAIR_CHECK_EVERY = 300
# HOW LONG AFTER STARTUP BEFORE THE FIRST LOOK. Long enough that the daemon has bound its
# port and can serve searches while the repair runs; short enough that a user opening a stale
# app sees it start fixing itself rather than wonder whether it noticed.
REPAIR_FIRST_CHECK = 20
REPAIR_COOLDOWN = 900
# HOW LONG TO WAIT BEFORE RETRYING A FAILED REPAIR. Separate from the success cooldown,
# because "it worked, do not do it again soon" and "it broke, try again shortly" are different
# instructions and sharing one number means a one-off failure costs 15 minutes of staleness.
REPAIR_FAIL_BACKOFF = 120

ALLOWED_ORIGINS = {
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
}


# =============================================================================
# THE ENGINE, LOADED ONCE
# =============================================================================
# WHERE AN API KEY LIVES, AND WHY IT IS NOT IN THE ENVIRONMENT.
#
# The CLI reads OPENAI_API_KEY from the shell, which is right for a terminal and impossible for
# a bundled app: the Tauri shell spawns the daemon with no environment at all, so a key set in
# a shell profile never reaches it. Measured before this existed: `--ask` worked from a
# terminal and the app had no LLM path whatsoever.
#
# SO THE KEY IS A FILE, MODE 0600, IN THE INDEX DIRECTORY — the same place and the same
# protection as api.token. 0600 is not decoration: it is the difference between a secret the
# user owns and a secret every process running as any local user can read.
#
# AND THE API NEVER RETURNS IT. GET /api/llm reports whether a key is configured and its
# last four characters. A settings endpoint that echoes a secret turns every browser devtools
# session and every screenshot into a leak.
LLM_PATH = INDEX_DIR / "llm.json"


def llm_settings() -> dict:
    """Read the stored model settings. Returns {} when nothing is configured."""
    try:
        if LLM_PATH.exists():
            import json as _json
            d = _json.loads(LLM_PATH.read_text(encoding="utf-8"))
            # THE FILE WINS OVER THE ENVIRONMENT for the daemon, because this is the copy
            # the user set through the app. The CLI still prefers the environment, so a
            # developer running from a shell is not surprised by a stale saved key.
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def save_llm_settings(d: dict) -> None:
    import json as _json
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LLM_PATH.with_suffix(".tmp")
    tmp.write_text(_json.dumps(d), encoding="utf-8")
    # 0600 BEFORE THE RENAME, not after. Creating the file readable and tightening it
    # afterwards leaves a window in which it is world-readable, and on a crash the window
    # never closes.
    os.chmod(tmp, 0o600)
    os.replace(tmp, LLM_PATH)


# PROVIDER PRESETS, BECAUSE ASKING FOR THREE FIELDS IS ASKING FOR TWO TOO MANY.
#
# The first version of this screen wanted an API key, an endpoint AND a model name. That is a
# form built by someone who already knows the answers: it asks a user to recall that DeepSeek's
# endpoint is `api.deepseek.com` and that its model is called `deepseek-chat`, and to get both
# right with no feedback until something fails.
#
# NOBODY KNOWS THAT, AND NOBODY SHOULD HAVE TO. Choosing which company you pay is one
# decision. Everything else is a lookup.
#
# `local` ENTRIES HAVE NO KEY AT ALL — that is the point of them. A local model is the
# option for someone who does not want their files leaving the machine, and a key field would
# contradict the only reason to choose it.
PROVIDERS = [
    {"id": "ollama", "label": "Ollama (on this computer)", "base_url": "http://localhost:11434/v1",
     "model": "", "needs_key": False, "local": True, "probe": "http://localhost:11434"},
    {"id": "lmstudio", "label": "LM Studio (on this computer)", "base_url": "http://localhost:1234/v1",
     "model": "", "needs_key": False, "local": True, "probe": "http://localhost:1234"},
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
     "model": "gpt-4o-mini", "needs_key": True, "local": False, "probe": ""},
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com",
     "model": "deepseek-chat", "needs_key": True, "local": False, "probe": ""},
    # Anthropic's API is NOT OpenAI-compatible on the wire: it uses /v1/messages
    # with a different body and an `x-api-key` header. It is listed here so a user
    # can see it and pick it, and the request builder below translates for it -
    # offering it and then sending an OpenAI-shaped request would fail with a 400
    # that reads like a bad key.
    {"id": "anthropic", "label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com",
     "model": "claude-sonnet-4-6", "needs_key": True, "local": False, "probe": ""},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "model": "", "needs_key": True, "local": False, "probe": ""},
    {"id": "custom", "label": "Something else…", "base_url": "", "model": "",
     "needs_key": True, "local": False, "probe": ""},
]


def local_models(probe: str) -> list:
    """ASK THE LOCAL SERVER WHAT IS INSTALLED, RATHER THAN ASKING THE USER TO SPELL IT.

    Someone running Ollama already has models — they downloaded them. Making them type the name
    from memory is asking them to repeat work they have already done, and to get it exactly
    right. Both Ollama and LM Studio expose a list; one is an API route, the other is
    OpenAI-compatible.
    """
    if not probe:
        return []
    import urllib.error
    import urllib.request
    for path in ("/api/tags", "/v1/models"):
        try:
            with urllib.request.urlopen(f"{probe.rstrip('/')}{path}", timeout=3) as r:
                d = json.load(r)
        except Exception:
            continue
        # Ollama answers with {"models":[{"name":...}]}, LM Studio and the OpenAI shape use
        # {"data":[{"id":...}]}. Both are accepted because the difference is theirs, not ours.
        out = [m.get("name") for m in (d.get("models") or []) if m.get("name")]
        out += [m.get("id") for m in (d.get("data") or []) if m.get("id")]
        if out:
            return sorted(set(out))
    return []


def llm_probe(timeout: int = 25) -> dict:
    """A SAVED KEY IS NOT A WORKING KEY, AND THE UI CANNOT TELL THE DIFFERENCE.

    Wrong key, wrong base URL, no credit, a model name that does not exist, and a network that
    blocks the endpoint all look identical from the settings form until the first real question
    fails — and then the failure is attributed to the app rather than to the key.

    SO THIS MAKES ONE TINY REQUEST AND REPORTS WHAT CAME BACK. It asks for a single word,
    which costs a fraction of a cent and proves three things at once: the key authenticates,
    the base URL resolves, and the model name is real.
    """
    cfg = llm_settings()
    key = cfg.get("api_key")
    if not key:
        return {"ok": False, "reason": "no API key saved"}
    base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("model") or "gpt-4o-mini"

    # One helper, two wire formats. Anthropic uses /v1/messages and x-api-key;
    # everything else imitates OpenAI. Branching once here is what makes adding a
    # provider a line rather than a refactor.
    from .llm import chat as _chat
    res = _chat(base, model, key,
                [{"role": "user", "content": "Reply with the word: ok"}],
                max_tokens=200, timeout=timeout)
    if res["error"]:
        return {"ok": False, "reason": res["error"].split(":")[0],
                "detail": res["error"][:200]}
    txt = res["text"]

    return {"ok": bool(txt), "model": model,
            "reason": "" if txt else "the model returned no content"}


def index_command() -> list[str]:
    """The command that re-runs the indexer, correct in both frozen and normal mode.

    `sys.executable` is the Python interpreter when running from source, and the
    frozen binary when running as a shipped app. Using it blindly means a shipped
    app runs `<binary> -m device_search.cli index`, which the binary does not
    understand. The fallback that made it appear to work was worse: it found a
    system python3 with none of the media packages and re-indexed every document as
    plain text. Measured: a 48-minute crawl under /Library/Frameworks/Python that
    had no pdftext and no OCR, overwriting the extractions that did.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "index"]
    return [sys.executable, "-u", "-m", "device_search.cli", "index"]


def estimate_folder(path: str, budget_s: float = 1.5) -> dict:
    """Count files and bytes under a path, with a time budget.

    Needed only on a fresh install, where the database is empty and every folder
    costs "0 files, 0 bytes" - which is true and useless, because the whole point
    of the tree is showing what a choice will cost BEFORE it is made.

    The walk is bounded. A home directory takes minutes, and a setup screen that
    hangs is worse than one showing an estimate. When the budget runs out the
    result is marked incomplete rather than reported as a total.
    """
    import os
    import time
    from .config import AUDIO_EXTS, SKIP_DIR_NAMES, VIDEO_EXTS

    files = 0
    size = 0
    complete = True
    t0 = time.time()

    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for n in names:
            files += 1
            try:
                size += os.stat(os.path.join(root, n)).st_size
            except OSError:
                pass
        if time.time() - t0 > budget_s:
            complete = False
            break

    return {"files": files, "text_bytes": size,
            "index_bytes": int(size * 1.4), "complete": complete}


def counts_by_ext(store) -> dict:
    """Files and bytes per extension, from an indexed column.

    This used to loop over every row in Python - 866,000 of them - and Python
    threads share one GIL, so the loop blocked every concurrent request. The UI
    polls the endpoints that need this, so the daemon spent most of its time here
    and searches appeared to hang.

    `documents.ext` is populated at insert and backfilled once on first open, so
    this is now a GROUP BY over an index.
    """
    out: dict[str, dict] = {}
    # Plain comparisons, not COALESCE: a function on the column stops SQLite from
    # using the covering index and costs an extra 1.2 seconds.
    for ext, n, total in store.conn.execute(
            "SELECT ext, COUNT(*), SUM(size) FROM documents "
            "WHERE is_dir = 0 AND ext IS NOT NULL GROUP BY ext"):
        if ext:
            out[ext] = {"files": n, "bytes": total}
    return out


_COUNT_CACHE: dict = {"at": 0.0, "data": {}}
# Text volume is the expensive one: SUM(LENGTH(text)) reads every document body,
# measured at 5.7 seconds over 865,000 documents. It changes only when the index
# does, so it is cached alongside the counts.
_CORPUS_CACHE: dict = {"at": 0.0, "text": 0, "files": 0}


def counts_cached(store) -> dict:
    """counts_by_ext, memoised for a minute.

    The query takes about 2 seconds on 865,000 rows because the extension has to be
    computed per row. It only changes when the index does, and the settings panel
    asks for it on every tab switch - so paying 2s each time is paying repeatedly
    for an answer that has not moved.
    """
    import time as _t
    if _t.time() - _COUNT_CACHE["at"] < 60 and _COUNT_CACHE["data"]:
        return _COUNT_CACHE["data"]
    _COUNT_CACHE["data"] = counts_by_ext(store)
    _COUNT_CACHE["at"] = _t.time()
    return _COUNT_CACHE["data"]


def total_for(counts: dict, exts: set) -> tuple[int, int]:
    files = sum(counts.get(e, {}).get("files", 0) for e in exts)
    nbytes = sum(counts.get(e, {}).get("bytes", 0) for e in exts)
    return files, nbytes


def folder_costs(store, parent: str | None = None) -> list[dict]:
    """Immediate subfolders of `parent`, with what each one would cost to index.

    The numbers come from the database rather than from walking the disk: a walk
    of a home directory takes minutes and would make the checkboxes feel broken,
    while a GROUP BY returns in milliseconds. The figure shown is what has actually
    been indexed under that path, which is the number the user is deciding about.

    `parent=None` returns the top-level entries of the home directory, so the first
    screen offers a choice rather than a list of nothing.
    """
    import os
    from .config import SKIP_DIR_NAMES, MAX_FILE_BYTES

    base = parent or str(store.get_meta("scan_root") or os.path.expanduser("~"))
    base = base.rstrip("/") or "/"
    prefix = base + "/"

    # IN SQL, NOT IN PYTHON.
    #
    # This fetched every path under the prefix - 597,220 rows for the home
    # directory - and split them one at a time to find the immediate child. Measured
    # at 7.9 seconds, for a list of 58 folders. The same question as a GROUP BY over
    # a substring is a single pass inside SQLite, and it never brings the rows across.
    #
    # `substr(path, n)` is 1-based, so n is the length of the prefix plus one.
    counts: dict[str, dict] = {}
    for child, files, total in store.conn.execute(
            "SELECT substr(rest, 1, instr(rest, '/') - 1) AS child, "
            "       COUNT(*), COALESCE(SUM(size), 0) "
            "  FROM (SELECT substr(path, ?) AS rest, COALESCE(size, 0) AS size "
            # A RANGE, NOT A LIKE. `path LIKE 'prefix%'` is case-insensitive, so
            # SQLite cannot seek: it SCANS the whole path index and filters. The
            # range form is a SEARCH. Measured on 866,237 documents: 8.0 s -> 0.14 s.
            "          FROM documents WHERE path >= ? AND path < ?) "
            " WHERE instr(rest, '/') > 0 "
            " GROUP BY child",
            (len(prefix) + 1, prefix, prefix + "\U0010ffff")):
        counts[child] = {"files": files, "bytes": total}

    out = []
    for name, d in sorted(counts.items(), key=lambda kv: -kv[1]["files"]):
        if not name:
            continue
        full = prefix + name
        excluded = name in SKIP_DIR_NAMES
        # The index stores the text plus the word index, measured at about 1.4x.
        # Time is the crawl; embedding is separate and shown as its own column.
        index_bytes = int(d["bytes"] * 1.4)
        entry = {
            "name": name,
            "path": full,
            "files": d["files"],
            "text_bytes": d["bytes"],
            "index_bytes": index_bytes,
            "excluded": excluded,
            "reason": "excluded by default" if excluded else "",
            "parent": base,
        }
        out.append(entry)
    return out


# Fingerprint map used by the staleness check. Module-level so it survives across
# requests; see the note where it is filled.
_FINGERPRINT_CACHE: dict = {"at": 0.0, "map": {}}


class Engine:
    """MODULE-LEVEL STATE IS THE POINT, NOT A SMELL. The whole reason this is a daemon is
    that loading the model is expensive; a per-request object would defeat it."""

    def __init__(self):
        self.store = Store(DB_PATH)
        self.semantic = None
        self.semantic_note = ""
        self._load_errors = []
        # THE INDEX RUN STATE LIVES ON THE ENGINE, because that is the object the HTTP handler
        # can actually see. Putting it on the server and reading it from the engine was a real
        # AttributeError that only surfaced when the endpoint was called.
        self.last_index: dict = {}
        self.deep_index_state: dict = {}
        self.media_build_state: dict = {}
        # AUTO-REPAIR, WITH A POLICY RATHER THAN A TRIGGER.
        #
        # The machinery to repair stale vectors has existed for several commits — plan_resume
        # finds the invalid shards, drop_shards removes them, the shards are resumable so only
        # those re-embed. What was missing was anything that DECIDED to run it.
        #
        # AND A NAIVE TRIGGER WOULD BE WRONG. "Repair whenever anything drifted" sounds
        # obviously right and is not: a browser writing cache, an editor writing swap files, any
        # log rotating — each changes a shard, and the index would re-embed CONTINUOUSLY and
        # never settle, competing with every query for CPU.
        #
        # So the trigger is a POLICY, and every clause is load-bearing:
        # 1. never while another run is active (two writers on one index)
        # 2. never more often than the cooldown (a repair is minutes of CPU)
        # 3. only when something is actually stale
        # 4. and it can be switched off entirely
        self.auto_repair = os.environ.get("DEVICE_SEARCH_AUTO_REPAIR", "1") != "0"
        self._last_repair = 0.0
        self._repair_note = "not needed yet"
        # THE CHILD IS HELD SO ITS DEATH CAN BE SEEN. A repair that starts and dies silently
        # is indistinguishable from one that is running: the banner says "indexing", the index
        # never changes, and the user waits for a job that no longer exists.
        self._repair_proc = None
        self._repair_failures = 0

    def warm(self) -> None:
        """Load the embedding backend up front so the FIRST query is not the slow one."""
        try:
            from .semantic import Semantic
            sem = Semantic()
            ok, why = sem.available()
            if ok and sem.load(VEC_PATH, IDS_PATH):
                sem.load_ok = True
                self.semantic = sem
                self.semantic_note = why
            else:
                self.semantic_note = why if not ok else "vectors not built"
        except Exception as e:
            self.semantic_note = f"{type(e).__name__}: {e}"
            self._load_errors.append(self.semantic_note)

    def _llm_env(self) -> dict:
        """Export the stored key into the environment FOR THIS PROCESS ONLY.

        answer.py and agent.py read OPENAI_* from os.environ — a convention the CLI depends on.
        Rather than give them a second code path that only the daemon uses, the daemon loads
        the saved settings into its own environment once. One convention, two ways to fill it.
        """
        cfg = llm_settings()
        if cfg.get("api_key"):
            os.environ["OPENAI_API_KEY"] = cfg["api_key"]
            if cfg.get("base_url"):
                os.environ["OPENAI_BASE_URL"] = cfg["base_url"]
            if cfg.get("model"):
                os.environ["OPENAI_MODEL"] = cfg["model"]
        return cfg

    def media_hits(self, query: str, limit: int = 8):
        """Images and video frames matching a DESCRIPTION of what they show.

        Separate from the text backends because it is a different question: bge-small
        embeds characters and has never seen a pixel. SigLIP embeds the image itself
        into the same space as the sentence, so "a red car near a house" can be
        compared against a photograph directly.

        Loaded lazily and cached: each index is a `.npy` file and a model load, and
        most searches never touch them. A missing index returns nothing rather than
        an error - there is nothing to search yet, which is a normal first-run state.
        """
        out = []
        if not getattr(self, "_img_index", None):
            try:
                from .image_search import ImageIndex
                idx = ImageIndex()
                if idx.load(INDEX_DIR / "images"):
                    self._img_index = idx
                else:
                    self._img_index = False
            except Exception:
                self._img_index = False
        if self._img_index:
            try:
                for doc_id, score in self._img_index.search(query, k=limit):
                    out.append((doc_id, score, "image", None))
            except Exception:
                pass

        if not getattr(self, "_vid_index", None):
            try:
                from .video import VideoIndex
                idx = VideoIndex()
                if idx.load(INDEX_DIR / "video"):
                    self._vid_index = idx
                else:
                    self._vid_index = False
            except Exception:
                self._vid_index = False
        if self._vid_index:
            try:
                for doc_id, ts, score in self._vid_index.search(query, k=limit):
                    out.append((doc_id, score, "video", ts))
            except Exception:
                pass
        return out

    def search(self, query: str, k: int = 10, use_llm: bool = False, ask: bool = False,
               rerank: bool = False, deep: bool = False,
               scope: str = "all") -> dict:
        from .agent import search as agent_search
        t0 = time.time()
        # A DEEPER SEARCH IS A DIFFERENT QUERY, NOT THE SAME ONE LOUDER.
        #
        # The first pass answers "does anything contain these words". When it comes
        # back thin there are two different reasons, and each needs a different
        # action: the words are too specific, or the file is a picture and has no
        # words at all. `deep` widens the query AND asks the image and video
        # indexes, which the ordinary search already does - so the widening is the
        # part that is new here.
        search_query = query
        relaxed = False
        if deep:
            words = [w for w in query.split() if len(w) > 2]
            if len(words) > 2:
                # The two longest words are the least common in practice; keeping
                # them and dropping the rest widens recall without reducing the
                # query to stopwords.
                search_query = " ".join(sorted(words, key=len, reverse=True)[:2])
                relaxed = True

        cands, trace = agent_search(search_query, self.store, semantic=self.semantic,
                                    use_llm=use_llm, top_k=k)
        if deep:
            trace["deep"] = {"relaxed_to": search_query if relaxed else query}

        # THE ANSWER, AND IT IS BUILT HERE RATHER THAN IN agent.py ON PURPOSE.
        #
        # agent.search() RETRIEVES. Turning a list into a sentence is a different operation with
        # a different failure mode — a sentence is believed, a list is checked — and folding
        # generation into retrieval would mean every caller got a model call whether it wanted
        # one or not.
        #
        # AND IT IS BEST-EFFORT: a missing key, a failed request or an empty result set leaves
        # `answer` absent and the results untouched. Searching must never break because the
        # optional half is unavailable.
        answer = None
        # THE SERVER DECIDES WHETHER TO ANSWER, NOT THE USER.
        #
        # The interface used to have an "answer" toggle the user had to find and turn on. That
        # is work for something the app can tell from the query: "how does the autoloader work"
        # is a question, "RetryMiddleware" is not. Asking the user to classify their own input
        # before typing it is the opposite of doing the least work.
        #
        # AND IT IS SAFE BECAUSE THE ANSWER IS NOT THE ONLY OUTPUT. The result list is
        # returned either way, every claim carries a citation, and the answer renders above its
        # own evidence. A wrong answer is checkable in one glance.
        kind_now = trace.get("plan", {}).get("kind")
        wants_answer = ask and (kind_now == "question" or ask == "force")
        if wants_answer and cands:
            try:
                from .answer import ask as ask_model, build_context
                def _text(c):
                    row = self.store.by_id(c.doc_id)
                    return (row[4] if row else "") or ""
                srcs, rep = build_context(cands, _text)
                # The context budget is reported on every answer, not only when it
                # bites: a user seeing 4 of 6,000 tokens knows there was room;
                # one seeing 6,000 of 6,000 and 5 truncated knows there was not.
                trace["context"] = {
                    "tokens_used": rep["tokens_used"],
                    "budget": rep["budget"],
                    "sources": rep["sent"],
                    "truncated": rep["truncated"],
                    "dropped_duplicate": rep["dropped_duplicate"],
                    "dropped_over_quota": rep["dropped_over_quota"],
                }
                if rep["blocked"]:
                    # THE INTERLOCK IS REPORTED, NOT SILENT. A user whose snippet was withheld
                    # needs to know the answer is based on less than everything that matched.
                    trace["secret_blocked"] = rep
                if srcs:
                    a = ask_model(query, srcs)
                    if not a.get("error"):
                        answer = {"text": a["answer"], "citations": a.get("citations", []),
                                  "refused": a.get("refused", False),
                                  "invented": a.get("invented_citations") or [],
                          "uncited": bool(a.get("uncited")),
                          # Travels WITH the answer, because that is where the
                          # interface reads it. Left in `trace`, it was returned
                          # to the client and never displayed.
                          "context": trace.get("context") or {}}
                    else:
                        trace["answer_error"] = a["error"]
                else:
                    trace["answer_error"] = ("nothing could be sent to the model — every "
                                             "matching snippet was withheld or empty")
            except Exception as e:
                trace["answer_error"] = f"{type(e).__name__}: {e}"

        # RE-RANKING, AND WHY IT IS A BUTTON RATHER THAN A STEP.
        #
        # A cross-encoder reads (query, document) TOGETHER and orders far better than the
        # bi-encoder that found them — but it is one forward pass PER CANDIDATE, about eight per
        # second on this CPU. Running it automatically would add five seconds to every
        # keystroke, which turns a responsive search box into a broken one.
        #
        # AND IT CANNOT ADD A RESULT. It reorders what retrieval found, so if the answer is
        # not in the list, re-ranking does not find it — only moves the best of what is there to
        # the top. Reported, because a user who clicks it and sees the same files deserves to
        # know that was the expected outcome and not a failure.
        rerank_report = None
        if rerank and cands:
            try:
                from .answer import MAX_CHARS_PER_SOURCE
                from .rerank import DEFAULT_TOP_N, Reranker, rerank_candidates
                def _txt(c):
                    row = self.store.by_id(c.doc_id)
                    return (row[4] if row else "") or ""
                snips = {c.doc_id: _txt(c)[:MAX_CHARS_PER_SOURCE]
                         for c in cands[:DEFAULT_TOP_N]}
                rep = rerank_candidates(query, cands, snips, Reranker())
                rerank_report = {k: v for k, v in rep.items() if k != "worst_drop"}
            except Exception as e:
                rerank_report = {"error": f"{type(e).__name__}: {e}"}

        # MEDIA RESULTS ARE A SEPARATE TIER, NOT FUSED INTO THE RANKING.
        #
        # A BM25 score and a cosine similarity are not on the same scale - BM25 is
        # unbounded and typically 5-40, cosine here lands around 0.10-0.18 - so
        # merging them by score puts every text hit above every image, always. That
        # is not a ranking, it is a type system pretending to be one.
        #
        # They also answer different questions: a text hit means the file CONTAINS
        # what you typed; an image hit means the picture LOOKS like what you
        # described. Saying so is more useful than a single number.
        media = []
        try:
            for doc_id, score, kind, ts in self.media_hits(query, limit=8):
                # The scope NARROWS. "Documents" means the caller does not want
                # pictures in the list, which is the only job a filter has - it
                # never enables a backend, because every backend already ran.
                if scope == "text":
                    continue
                if scope == "image" and kind != "image":
                    continue
                if scope in ("video", "audio") and kind != "video":
                    continue
                row = self.store.by_id(doc_id)
                if not row:
                    continue
                media.append({
                    "path": row[1],
                    "kind": kind,
                    "score": round(float(score), 4),
                    "at_seconds": ts,
                    "at": (f"{int(ts)//60}:{int(ts)%60:02d}" if ts is not None else None),
                })
        except Exception as e:
            trace["media_error"] = f"{type(e).__name__}: {e}"

        home = str(Path.home())
        return {
            "rerank": rerank_report,
            "media": media,
            "answer": answer,
            # THE REASON IS RETURNED, NOT KEPT IN THE TRACE.
            #
            # `answer` was forwarded and `answer_error` was not, so a failed generation came back
            # as `answer: null` with no explanation — which is indistinguishable from "the model
            # was never asked". The user sees a question produce no answer and has no way to find
            # out whether that is a missing key, a bad model name, or a network problem.
            "answer_error": trace.get("answer_error", ""),
            # AN EMPTY FILTER RESULT IS AN ANSWER, AND IT HAS TO BE SAID OUT LOUD. Without this
            # the user sees an empty list and cannot tell it from "the search is broken".
            "meta": trace.get("meta", {}),
            # The interface needs to know when a fragment query was answered
            # WITHOUT the code index, so it can offer to build it instead of
            # leaving the user with a slow or thin result and no explanation.
            "exact_index": trace.get("exact_index"),
            # What the deep pass actually did. "Search deeper" has to be able to
            # show what it changed, or it is indistinguishable from a re-run of the
            # same query that happened to return something this time.
            "deep": trace.get("deep"),
            "query": query,
            "took_ms": int((time.time() - t0) * 1000),
            "kind": trace["plan"]["kind"],
            "reasons": trace["plan"]["reasons"],
            "backends": trace["plan"]["backends"],
            "broadened": trace.get("broadened", False),
            "llm": trace.get("llm", {}),
            "results": [{
                "path": c.path.replace(home, "~", 1),
                "full_path": c.path,
                "line": c.line_no,
                # `lexical` IS THE TWO-TIER FLAG AND THE UI MUST HONOUR IT. A semantic hit is
                # not evidence the thing exists; the measured score distributions overlap so
                # completely that no threshold separates them (a garbage query scored 0.649, a
                # real one 0.632). The UI must not render the two lists identically.
                "lexical": c.lexical,
                "lang": c.lang,
                "snippet": c.snippet,
                "via": c.sources,
            } for c in cands],
        }

    def coverage(self, query: str = "", kind: str = "") -> list[dict]:
        """What is NOT searchable, why, and what would fix it.

        A search that says "no results" when half the corpus was never embedded is
        correct and useless. This returns the gaps relevant to the query in hand,
        each with the action that closes it, so the interface can offer the next
        step instead of leaving the user to guess whether the file is absent or
        merely unexamined.

        Only gaps that bear on THIS query are returned. A filename search does not
        need the image index, and offering it would be noise.
        """
        out: list[dict] = []
        ql = (query or "").lower()

        wants_meaning = kind in ("question", "prose") or any(
            w in ql for w in ("how ", "why ", "what ", "explain", "meaning", "about"))
        wants_media = any(w in ql for w in (
            "image", "picture", "photo", "screenshot", "chart", "video", "audio",
            "recording", "speech", "said", "shows", "looks like", "describe"))
        wants_code = kind == "code"

        # 1. MEANING
        try:
            from .vectors import VectorStore
            from .config import INDEX_DIR as _ID
            covered = len(VectorStore(_ID, dim=384).man.done_doc_ids)
        except Exception:
            covered = 0
        whole = self.store.count()
        if wants_meaning and whole and covered < whole * 0.98:
            out.append({
                "key": "meaning",
                "label": "Meaning search",
                "covered": covered,
                "total": whole,
                "detail": f"{covered:,} of {whole:,} documents have been read for meaning",
                "why": "Documents without vectors can still be found by keyword and by name, "
                       "but not by a question that shares no words with them.",
                "action": {"label": "Index meaning", "panel": "ai"},
            })

        # 2. PICTURES
        try:
            from .image_search import ImageIndex
            from .config import INDEX_DIR as _ID
            idx = ImageIndex(); idx.load(_ID / "images")
            img_done = len(idx.doc_ids)
        except Exception:
            img_done = 0
        img_total = total_for(counts_by_ext(self.store), IMAGE_EXTS)[0]
        if wants_media and img_total and img_done < img_total * 0.98:
            out.append({
                "key": "images",
                "label": "Searching pictures by description",
                "covered": img_done,
                "total": img_total,
                "detail": f"{img_done:,} of {img_total:,} pictures have been described",
                "why": "The rest can still be found by their filename and by any text "
                       "recovered from them, but not by describing what is in them.",
                "action": {"label": "Describe pictures", "panel": "media"},
            })

        # 3. VIDEO AND AUDIO, which have never been run on this machine
        for key, label, exts, panel, verb in (
                ("video", "Searching video", VIDEO_EXTS, "media", "Sample frames"),
                ("audio", "Searching audio", AUDIO_EXTS, "media", "Transcribe")):
            total = total_for(counts_by_ext(self.store), exts)[0]
            if not total:
                continue
            done = self.store.conn.execute(
                "SELECT COUNT(*) FROM documents WHERE method IN (?,?) "
                "AND ext IN (%s)" % ",".join("?" * len(exts)),
                ("whisper" if key == "audio" else "frame",
                 "transcript", *exts)).fetchone()[0]
            if wants_media and done < total * 0.98:
                out.append({
                    "key": key, "label": label, "covered": done, "total": total,
                    "detail": f"{done:,} of {total:,} {key} files have been indexed",
                    "why": f"The rest can be found by filename, but not by what they "
                           f"{'show' if key == 'video' else 'say'}.",
                    "action": {"label": verb, "panel": panel},
                })

        # 4. CODE FRAGMENTS
        if wants_code and not self.store.trigram_ready():
            out.append({
                "key": "code",
                "label": "Finding exact code fragments",
                "covered": 0, "total": self.store.count(),
                "detail": "The code index has not been built",
                "why": "Without it, a fragment is found by scanning every document, "
                       "which takes about twenty seconds and may miss files.",
                "action": {"label": "Build the code index", "panel": "code"},
            })
        return out

    def index_status(self) -> dict:
        """WHAT THE UI NEEDS TO SAY "indexing 6% — results are incomplete".

        THE UI CANNOT COMPUTE THIS ITSELF. Only the daemon knows which documents are in the
        index, which are embedded, and whether the vectors still describe the documents. A
        frontend guessing at it would show a confident progress bar attached to nothing.
        """
        from .vectors import VectorStore
        from .config import INDEX_DIR
        vs = VectorStore(INDEX_DIR, dim=384)
        doc_count = self.store.count()
        # The same fingerprint the manifest stored, recomputed over the CURRENT index, so the
        # comparison is like-for-like.
        row = self.store.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(mtime),0) FROM documents"
        ).fetchone()
        # THE PER-SHARD CHECK DECIDES, NOT THE GLOBAL ONE.
        #
        # `check_stale` compares a GLOBAL fingerprint — document count, max id, sum of mtimes —
        # against what `finish()` stamped at the end of a run. `plan_resume` compares PER-SHARD
        # fingerprints: which documents each shard actually contains.
        #
        # AND THEY CAN DISAGREE, WHICH IS WHAT MADE THIS SO CONFUSING. Measured right now:
        #
        # plan_resume() -> 3 invalid shards, 750 documents to re-embed
        # check_stale() -> stale: False, "vectors match the index"
        #
        # Because `finish()` stamps the global fingerprint from the CURRENT store whether or not
        # the shards cover it. So a run that stopped part-way, or a document removed after the
        # shards were written, leaves the coarse check saying "fine" while 750 documents have
        # vectors that describe something else.
        #
        # THE COARSE CHECK IS A SUMMARY AND IT WAS BEING TRUSTED AS THE ANSWER. The per-shard
        # one is the truth, because it is the one the repair actually acts on.
        stale = vs.check_stale(row[0], row[1], float(row[2] or 0))
        try:
            # COLUMN INDICES MUST MATCH THE SELECT. This said r[3] for a 3-column query, so it
            # raised IndexError on every call — and the handler below reported the result as
            # "not stale", which is the worst possible presentation of "the check did not run".
            # CACHED, BECAUSE THIS READS EVERY DOCUMENT'S TEXT.
            #
            # `LENGTH(text)` over 866,000 documents reads about 6 GB. The interface
            # polls /api/health every few seconds, so this ran constantly and every
            # request took 30 seconds - the daemon was permanently busy measuring
            # how stale it was.
            #
            # Staleness does not need to be real-time. A minute-old answer is the
            # same decision as a fresh one, and the cost of being 60 seconds late is
            # that a repair starts 60 seconds later.
            import time as _t
            now = _t.time()
            if now - _FINGERPRINT_CACHE["at"] > 300 or not _FINGERPRINT_CACHE["map"]:
                # PERSISTED, BECAUSE IT COSTS TWENTY SECONDS TO COMPUTE.
                #
                # The map is keyed by document id and needs each document's TEXT
                # LENGTH, which means reading about 6 GB. Measured at 21 seconds, on
                # the first /api/health call after every start - so the daemon was
                # answering "are you up?" twenty-six seconds after launch, and the
                # interface had already given up.
                #
                # The key is the document count plus the newest mtime: if either
                # moved, the index changed and the file is refused. A stale
                # fingerprint would report an index as current when it is not, which
                # is the one direction this check must never fail in.
                import json as _json
                from .config import INDEX_DIR as _IDIR
                fp_path = _IDIR / "fingerprint.json"
                stamp = self.store.conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(mtime),0), COALESCE(MAX(id),0) "
                    "FROM documents").fetchone()
                if fp_path.exists():
                    try:
                        saved = _json.loads(fp_path.read_text())
                        if saved.get("stamp") == list(stamp):
                            _FINGERPRINT_CACHE["map"] = saved["map"]
                            _FINGERPRINT_CACHE["at"] = now
                    except Exception:
                        pass
                if not _FINGERPRINT_CACHE["map"]:
                    _FINGERPRINT_CACHE["map"] = {
                        r[0]: f"{r[1] or 0:.3f}:{r[2]}"
                        for r in self.store.conn.execute(
                            "SELECT id, mtime, LENGTH(text) FROM documents")}
                    _FINGERPRINT_CACHE["at"] = now
                    try:
                        fp_path.write_text(_json.dumps(
                            {"stamp": list(stamp), "map": _FINGERPRINT_CACHE["map"]}))
                    except Exception:
                        pass   # a cache that cannot be written is not a failure
            cur = _FINGERPRINT_CACHE["map"]
            # THE POLICY MUST BE PASSED HERE, OR THE API CANNOT SEE A POLICY CHANGE AT ALL.
            # It reported "3 of 23 shards no longer match their documents" while the real answer
            # was "every shard was built by a policy we no longer use" — the two are different
            # facts and only the second explains why a trivial-looking drift needs 24 minutes.
            from .comments import policy_version
            plan = vs.plan_resume(cur, policy=policy_version())
            if plan["invalid_shards"]:
                if plan.get("policy_changed"):
                    reason = (f"the embedding policy changed ({plan['policy_changed']}) — "
                              f"every vector is rebuilt")
                else:
                    # TWO DIFFERENT FACTS, AND THE FIRST MESSAGE MERGED THEM.
                    #
                    # "1 of 23 shards no longer match (861,276 documents)" reads as
                    # though 861,276 documents have wrong vectors. They do not: the
                    # vectors cover `covered` documents, and 861,276 is what a
                    # rebuild to cover the whole index WOULD cost. One is a
                    # staleness fact, the other a coverage fact, and the reader
                    # cannot act on either when they are run together.
                    covered = len(vs.man.done_doc_ids)
                    whole = ENGINE.store.count()
                    reason = (
                        f"Semantic-search covers {covered:,} of {whole:,} documents, "
                        f"and {len(plan['invalid_shards'])} of {len(vs.man.shards)} "
                        f"of its shards are out of date. Exact, keyword and filename "
                        f"results are unaffected."
                    )
                stale = {"stale": True, "severity": "outdated",
                         "invalid_shards": len(plan["invalid_shards"]),
                         "documents": plan["will_reembed"], "reason": reason}
        except Exception as e:
            # A CHECK THAT CANNOT RUN MUST NOT REPORT SUCCESS.
            #
            # This previously did `stale.setdefault("note", ...)`, which left `stale: False` in
            # place. So an IndexError in the verification produced the SAME ANSWER as a healthy
            # index — **a check that fails silently is not a check**, and this one quietly
            # reported "vectors match the index" while 750 documents had wrong vectors.
            #
            # UNKNOWN IS NOW ITS OWN ANSWER, and it is treated as stale because the cost of
            # being wrong differs wildly: re-embedding a shard wastes CPU, while trusting wrong
            # vectors returns wrong results and says nothing.
            stale = {"stale": True, "severity": "unknown",
                     "reason": f"could not verify the vectors ({type(e).__name__}: {e}) — "
                               f"treating them as unverified until the check succeeds"}
        run = dict(self.last_index or {})
        emb = {"documents_embedded": len(vs.man.done_doc_ids),
               "documents_total": doc_count,
               "chunks": vs.total_chunks(),
               "shards": len(vs.man.shards),
               "complete": vs.man.complete,
               "model": vs.man.model, "runtime": vs.man.runtime}
        if emb["documents_total"]:
            emb["percent"] = round(emb["documents_embedded"] / emb["documents_total"] * 100, 1)
        else:
            emb["percent"] = 0.0
        # THE TERMINAL RUN IS THE AUTHORITY WHEN THERE IS ONE. `ds index` in a shell and
        # `POST /api/index` both write the same status file, so the UI reports either of them
        # identically — including the ETA, which the daemon could never compute on its own.
        from .vectors import read_status
        live = read_status(INDEX_DIR)
        return {"indexing": bool(live.get("running")) or run.get("running", False),
                "live": live, "run": run, "embedding": emb, "stale": stale,
                "searchable_now": doc_count > 0,
                # THE USER IS TOLD WHAT THE APP DOES ON ITS OWN. A background job that starts
                # itself and is not surfaced is indistinguishable from a machine that got slow.
                "auto_repair": {"enabled": self.auto_repair,
                                "last": self._last_repair, "note": self._repair_note,
                                "failures": self._repair_failures,
                                # So the UI can say "the repair died" instead of showing a
                                # progress bar for a process that is gone.
                                "child_alive": (self._repair_proc is not None
                                                and self._repair_proc.poll() is None)}}

    def outdated_reason(self) -> str:
        """"" when the index is current, otherwise WHY it is not — in the user's words.

        This is the answer to "would a user have to run a command". They should not: the daemon
        can tell that its own index is missing something, and the only thing it needs from the
        user is permission to spend their CPU, which it already has.
        """
        try:
            from .config import INDEX_FORMAT
            need, why = self.store.needs_rebuild_for(INDEX_FORMAT)
            return why if need else ""
        except Exception:
            return ""

    def maybe_repair(self) -> dict:
        """Spawn a repair run if the vectors have drifted. Called on a timer, never inline.

        IT SPAWNS A SEPARATE PROCESS, NOT A THREAD. Embedding is CPU-bound and would block or
        starve the request threads that serve searches. A subprocess can also be killed, resumed
        and observed — none of which is true of a thread inside the server.
        """
        if not self.auto_repair:
            return {"started": False, "reason": "auto-repair is off"}
        from .vectors import read_status
        live = read_status(INDEX_DIR)
        if live.get("running"):
            return {"started": False, "reason": "a run is already in progress"}
        # FIRST: DID THE LAST REPAIR DIE? Checked BEFORE the cooldown, because a repair that
        # failed must not be locked out for 15 minutes by the same timer that paces successful
        # ones. And the failure is surfaced rather than absorbed — the banner must not go on
        # claiming progress for a process that exited.
        if self._repair_proc is not None:
            code = self._repair_proc.poll()
            if code is None:
                return {"started": False, "reason": "the repair started earlier is still running"}
            self._repair_proc = None
            if code != 0:
                self._repair_failures += 1
                self._repair_note = (f"the last repair exited with code {code} "
                                     f"({self._repair_failures} failure(s))")
                # A SHORT BACKOFF AFTER FAILURE, not the full cooldown. Long enough not to
                # spin on a broken environment, short enough to recover from a one-off.
                self._last_repair = time.time() - (REPAIR_COOLDOWN - REPAIR_FAIL_BACKOFF)

        if time.time() - self._last_repair < REPAIR_COOLDOWN:
            left = int((REPAIR_COOLDOWN - (time.time() - self._last_repair)) / 60)
            return {"started": False,
                    "reason": f"cooldown, {left}m remaining"
                              + (f" — last attempt: {self._repair_note}"
                                 if self._repair_failures else "")}
        st = self.index_status()
        # "NOT STALE" IS NOT THE SAME AS "UP TO DATE", AND CONFLATING THEM IS THE WHOLE BUG.
        #
        # `stale` answers "have the files changed since the last index". It says nothing about
        # whether the index CONTAINS EVERYTHING THIS VERSION KNOWS HOW TO EXTRACT. An index with
        # no directories in it is perfectly not-stale and still cannot find a single folder.
        #
        # SO A FORMAT MISMATCH IS ALSO A REASON TO REBUILD, and the user is told why rather
        # than having their CPU spent silently. This is the answer to "would a user have to run a
        # command": no — the daemon notices on its own and rebuilds in the background while search
        # keeps working.
        outdated = self.outdated_reason()
        if not st["stale"].get("stale") and not outdated:
            self._repair_note = "up to date"
            return {"started": False, "reason": "up to date"}
        self._repair_note = outdated or self._repair_note

        log_path = DB_PATH.parent / "daemon.log"
        try:
            out = open(log_path, "a")
        except OSError:
            out = subprocess.DEVNULL
        cmd = index_command()
        try:
            proc = subprocess.Popen(cmd, stdout=out, stderr=out,
                                    cwd=str(Path(__file__).resolve().parent.parent))
        except Exception as e:
            self._repair_note = f"could not start: {e}"
            return {"started": False, "reason": self._repair_note}
        self._last_repair = time.time()
        self._repair_proc = proc
        self._repair_note = f"repairing ({st['stale'].get('reason', '')[:60]})"
        return {"started": True, "pid": proc.pid, "why": st["stale"].get("reason")}

    def stats(self) -> dict:
        s = self.store.stats()
        return {
            "documents": s["documents"],
            "text_mb": round(s["bytes"] / 1e6, 2),
            "lines": s["lines"],
            "mode": self.store.get_meta("mode"),
            "roots": self.store.get_meta("roots"),
            "langs": s["langs"][:10],
            "semantic": self.semantic_note,
        }


ENGINE: Engine | None = None


# =============================================================================
# HTTP
# =============================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "device-search/0.1"

    # ---- security --------------------------------------------------------
    def _authorised(self) -> bool:
        # HOST CHECK FIRST — this is the DNS-rebinding defence. A rebinding attack sends
        # `Host: evil.example.com`, so rejecting non-localhost Host headers stops it before the
        # token is even considered.
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host not in ("localhost", "127.0.0.1", "::1", ""):
            self._send(403, {"error": "forbidden host"})
            return False
        token = self.headers.get("X-DS-Token") or parse_qs(
            urlparse(self.path).query).get("token", [""])[0]
        if not secrets.compare_digest(token, self.server.token):
            self._send(401, {"error": "bad or missing token"})
            return False
        return True

    def log_message(self, fmt, *args): # quiet: a daemon logging every poll is noise
        pass

    def _read_body(self) -> str:
        """stdlib http.server does NOT read the body for you, and forgetting this makes every
        POST look like it arrived empty."""
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS, AND THE COMMENT THAT WAS HERE FOR SEVERAL ROUNDS WAS WRONG.
        #
        # It said: "The UI is served from this same origin, so it does not need CORS at all."
        # THAT IS FALSE. The Tauri webview runs at `tauri://localhost` — a CUSTOM SCHEME,
        # not http://127.0.0.1:8734. Different origin, so CORS applies, and omitting the headers
        # blocked the app's OWN interface while curl (which has no origin and no CORS) kept
        # working perfectly. That is why every terminal test I ran said the daemon was healthy.
        #
        # AND `X-DS-Token` IS WHY IT FAILED OUTRIGHT RATHER THAN PARTIALLY. A custom header
        # is not a "simple request", so the browser MUST send an OPTIONS preflight first. There
        # was no do_OPTIONS, so it got 501 and the real request was never sent.
        #
        # THE ORIGIN IS AN ALLOWLIST, NOT `*`. `Access-Control-Allow-Origin: *` would let ANY
        # web page the user visits read their file contents — the exact threat the Host-header
        # check exists to stop, reopened through a different door.
        self._cors()
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # ---- routes ----------------------------------------------------------
    def _cors(self) -> None:
        """EMITTED FROM ONE PLACE, because the preflight and the real response MUST agree.

        The first version of this fix set these headers inside `_send()` only, so `do_OPTIONS`
        returned a bare `204` with no `Access-Control-Allow-*` at all — and the browser rejects
        that exactly as it rejected the 501. A preflight that does not actually authorise the
        request it precedes is the same as no preflight.
        """
        origin = self.headers.get("Origin") or ""
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            # Vary matters: without it a cache can serve one origin's response to another.
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "X-DS-Token, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        """THE PREFLIGHT, AND ITS ABSENCE WAS THE ENTIRE BUG.

        The webview sends `OPTIONS` before every request carrying `X-DS-Token`. Without this
        handler the stdlib answers **501 Unsupported method**, the browser refuses to send the
        real request, and `fetch` rejects with WebKit's unhelpful "Load failed" — which is what
        the UI displayed as "daemon unreachable (loading failed)".

        It is answered WITHOUT a token check on purpose: a preflight carries no credentials
        by design, and the real request is still authenticated immediately afterwards.
        """
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/health":
            # UNAUTHENTICATED ON PURPOSE: the UI polls this to know the daemon is up, and it
            # reveals nothing about the user's files.
            st = ENGINE.index_status()
            self._send(200, {"ok": True, "version": "0.1",
                             "documents": ENGINE.store.count(),
                             "semantic": ENGINE.semantic_note,
                             # THE WARNING TRAVELS ON THE CHEAP POLL. The UI polls health
                             # every few seconds; making it also fetch /api/index/status just to
                             # know whether to draw a banner would double the polling for one
                             # boolean.
                             "stale": st["stale"],
                # THE INDEX IS OUT OF DATE FOR A REASON THE USER DID NOT CAUSE, and the app
                # says so and fixes it. It is reported separately from `stale`, because
                # "files changed since you last indexed" and "this index is missing a whole
                # category of thing" call for different words.
                "outdated_reason": ENGINE.outdated_reason(),
                             "embedding_percent": st["embedding"]["percent"],
                             "indexing": st["indexing"],
                             # The ETA and rate come from the process DOING the work. The
                             # daemon cannot derive them, so it must not invent them.
                             "live": {k: st["live"].get(k) for k in
                                      ("running", "percent", "rate", "eta_seconds", "stage",
                                         "files_seen", "elapsed_seconds", "note",
                                       "documents_done", "documents_total", "reason")}})
            return
        if not self._authorised():
            return
        q = parse_qs(url.query)
        try:
            if url.path == "/api/llm":
                # GET NEVER RETURNS THE KEY. It reports whether one is configured and its
                # last four characters — enough for a user to recognise which key is saved, and
                # not enough to use it. A settings endpoint that echoes a secret turns every
                # devtools session and every screenshot into a leak.
                if self.command == "GET":
                    cfg = llm_settings()
                    key = cfg.get("api_key") or ""
                    self._send(200, {"configured": bool(key),
                                     "hint": ("\u2026" + key[-4:]) if len(key) >= 4 else "",
                                     "base_url": cfg.get("base_url") or "",
                                     "model": cfg.get("model") or "",
                                     "ok": True})
                return

            if url.path == "/api/llm/providers":
                # EACH LOCAL ENTRY IS PROBED LIVE, so the panel can show "found 3 models"
                # rather than offering a choice that will fail. A dropdown listing a server that
                # is not running is a dropdown that produces an error message.
                out = []
                for p in PROVIDERS:
                    e = dict(p)
                    e["models"] = local_models(p["probe"]) if p.get("local") else []
                    e["available"] = bool(e["models"]) if p.get("local") else True
                    e.pop("probe", None)
                    out.append(e)
                cfg = llm_settings()
                e = {"providers": out, "base_url": cfg.get("base_url") or "",
                     "model": cfg.get("model") or "",
                     "configured": bool(cfg.get("api_key"))}
                self._send(200, e)
                return

            if url.path == "/api/llm/test":
                # A TEST BUTTON, BECAUSE A SAVED KEY IS NOT A WORKING KEY. Wrong key, wrong
                # base URL, no credit and a network that blocks the endpoint all look identical
                # from the UI until the first real question fails — and then the failure is
                # attributed to the app.
                self._send(200, llm_probe())
                return

            if url.path == "/api/search":
                query = (q.get("q") or [""])[0]
                if not query.strip():
                    self._send(400, {"error": "empty query"})
                    return
                # NO CAP. This was the outermost truncation of the lot: even if every layer
                # below had returned everything, nothing above 50 could reach the user. A
                # server that decides how many results a person may see is the tool deciding it
                # knows better than the question.
                k = max(1, int((q.get("k") or ["50"])[0]))
                llm = (q.get("llm") or ["0"])[0] == "1"
                # `ask` TURNS A LIST INTO AN ANSWER. It costs an API call and uploads snippets
                # (minus anything matching a secret pattern), so it is never the default — and it
                # fails with a reason rather than an empty box when no key is configured.
                ask = (q.get("ask") or ["0"])[0] == "1"
                rerank = (q.get("rerank") or ["0"])[0] == "1"
                deep = (q.get("deep") or ["0"])[0] == "1"
                scope = (q.get("scope") or ["all"])[0]
                ENGINE._llm_env()
                self._send(200, ENGINE.search(query, k=k, use_llm=llm, ask=ask,
                                              rerank=rerank, deep=deep, scope=scope))
            elif url.path == "/api/deep-index":
                # The interface needs the cost before it asks, for the same reason the
                # command line does: this is the only step that can use more disk than
                # the documents it indexes.
                # Cached: SUM(LENGTH(text)) reads every document body and takes
                # about six seconds. It only moves when the index does, and the tab
                # is re-opened every time the user visits Settings.
                import time as _t
                if _t.time() - _CORPUS_CACHE["at"] > 60 or not _CORPUS_CACHE["files"]:
                    row = ENGINE.store.conn.execute(
                        "SELECT COALESCE(SUM(LENGTH(text)),0), COUNT(*) FROM documents "
                        "WHERE COALESCE(is_dir,0)=0").fetchone()
                    _CORPUS_CACHE.update({"text": row[0], "files": row[1], "at": _t.time()})
                text, files = _CORPUS_CACHE["text"], _CORPUS_CACHE["files"]
                self._send(200, {
                    "available": True,
                    "documents": files,
                    "estimated_gb": round(max(text * 1.9 / 1e9, 0.01), 2),
                    "estimated_minutes": round(max(files / 975 / 60, 0.1), 1),
                    "free_gb": round(shutil.disk_usage(INDEX_DIR).free / 1e9, 1),
                    "built": ENGINE.store.trigram_ready(),
                })
            elif url.path == "/api/media":
                # The cost of the two media jobs, for the same reason the code
                # index shows its cost: both take minutes and one of them writes
                # gigabytes, so neither may start without the number being visible.
                from .config import AUDIO_EXTS, VIDEO_EXTS
                from .vectors import read_status
                # NO PER-FILE DURATION LOOKUP HERE.
                #
                # The first version opened every video with av to count its frames.
                # Measured on 131 videos: the request never returned, the interface
                # sat on "Checking the cost…" and the tab looked broken. Opening a
                # container is a file read, and 131 of them inside an HTTP handler is
                # a hang, not a cost estimate.
                #
                # Counts come from the database and are instant. The time estimate
                # is derived from size rather than measured, and labelled as an
                # estimate - which is what it always was.
                counts = counts_cached(ENGINE.store)
                n_audio, _ = total_for(counts, AUDIO_EXTS)
                n_video, video_bytes = total_for(counts, VIDEO_EXTS)
                IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
                n_img, _ = total_for(counts, IMG)
                # ~0.19 MB per second of video, MEASURED on this corpus: 2.80 GB
                # across 131 recordings that produced 1,458 frames at one per 10s.
                #
                # The first constant was 3 MB/s, guessed from "screen recording" -
                # which gave 93 frames where the truth is 1,458. Frames are only a
                # time estimate here, so the cost of being wrong was a wildly
                # optimistic "2 minutes" for a job that takes seven.
                frames = max(int(video_bytes / 190_000 / 10), 0)
                self._send(200, {
                    "images": n_img,
                    "image_index_built": (INDEX_DIR / "images" / "image_vectors.npy").exists(),
                    "image_minutes": round(n_img * 0.22 / 60, 1),
                    "audio": n_audio,
                    "video": n_video,
                    "frames": frames,
                    "media_minutes": round(frames * 0.19 / 60 + 2, 1),
                    "video_index_built": (INDEX_DIR / "video" / "video_vectors.npy").exists(),
                    # So the tab can say "indexing, 40% done, 12 minutes left"
                    # instead of "Running in the background" and then silence.
                    "building": dict(ENGINE.media_build_state),
                    "status": read_status(INDEX_DIR),
                })
            elif url.path == "/api/stats":
                self._send(200, ENGINE.stats())
            elif url.path == "/api/coverage":
                # WHAT IS NOT SEARCHABLE, AND WHAT TO DO ABOUT IT. The interface asks
                # after every search, with the query and its kind, so only the gaps
                # that bear on this search come back.
                qc = parse_qs(url.query)
                self._send(200, {"gaps": ENGINE.coverage(
                    (qc.get("q") or [""])[0], (qc.get("kind") or [""])[0])})
            elif url.path == "/api/folders":
                q = parse_qs(url.query)
                parent = (q.get("path") or [None])[0]
                self._send(200, {"folders": folder_costs(ENGINE.store, parent),
                                 "parent": parent or str(ENGINE.store.get_meta("scan_root") or "")})
            elif url.path == "/api/roots":
                from pathlib import Path as _P

                from .config import MODES
                configured = ENGINE.store.get_meta("roots") or []
                # SUGGESTED FOLDERS ARE CHECKED FOR EXISTENCE, so the UI never offers a
                # folder the user does not have. An empty Downloads on a fresh machine is normal.
                # With an estimate, because on a fresh install these are the only
                # folders the setup screen can offer and "0 files" is not a choice
                # anyone can make. Bounded walk: see estimate_folder.
                suggested = []
                for f in (_P.home() / "Documents", _P.home() / "Desktop", _P.home() / "Downloads"):
                    entry = {"path": str(f), "name": f.name, "exists": f.is_dir()}
                    if entry["exists"]:
                        entry.update(estimate_folder(str(f)))
                    suggested.append(entry)
                self._send(200, {"configured": configured,
                                 "indexed": ENGINE.store.roots_in_index(),
                                 "suggested": suggested,
                                 "modes": sorted(MODES.keys()),
                                 "documents": ENGINE.store.count()})
            elif url.path == "/api/index/status":
                self._send(200, ENGINE.index_status())
            elif url.path == "/api/runtime":
                # THE CHOOSER, SERVED. The UI cannot run pip itself and must not try — it
                # has no idea which interpreter is running the engine, and installing into the
                # wrong one succeeds while changing nothing.
                from .runtime import options, survey
                est = 0
                n = ENGINE.store.count()
                if n:
                    est = int(n * 34) # measured mean chunks/doc
                self._send(200, {"survey": survey(), "options": options(est),
                                 "estimated_chunks": est})
            elif url.path == "/api/install/status":
                from .runtime import INSTALLER
                self._send(200, INSTALLER.snapshot())
            else:
                self._send(404, {"error": "no such route"})
        except Exception as e:
            # ERRORS ARE RETURNED, NOT SWALLOWED. A UI that shows an empty list when the
            # backend threw is indistinguishable from "not found" — the exact failure this
            # project keeps recording.
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._authorised():
            return
        path_q = urlparse(self.path)

        if path_q.path == "/api/build-media":
            """Start an image or media build in a thread."""
            import json as _json
            import threading
            body = {}
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    body = _json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            kind = (body or {}).get("kind") or "images"

            state = ENGINE.media_build_state
            if state.get("running"):
                self._send(200, {"started": False, "reason": "already running"})
                return
            state.update({"running": True, "kind": kind, "error": None, "finished": False})

            from .vectors import write_status

            def _run():
                try:
                    if kind == "images":
                        from .image_search import ImageIndex
                        IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
                        rows = [(i, p) for i, p in ENGINE.store.conn.execute(
                            "SELECT id, path FROM documents WHERE COALESCE(is_dir,0)=0")]
                        items = [(i, p) for i, p in rows if Path(p).suffix.lower() in IMG]
                        # Progress goes into the SAME status file the crawl uses,
                        # so the existing banner shows it without the interface
                        # needing a second progress mechanism.
                        total = len(items)
                        t0 = time.time()

                        def _say(done, rate):
                            left = (total - done) / max(rate, 1e-6)
                            write_status(
                                INDEX_DIR, stage="images", finished=False,
                                documents_total=total, documents_done=done,
                                elapsed_seconds=round(time.time() - t0),
                                percent=round(done / max(total, 1) * 100, 1),
                                eta_seconds=int(left))
                            state["done"] = done
                            state["total"] = total

                        idx = ImageIndex()
                        idx.embed_images(items, on_progress=_say)
                        idx.save(INDEX_DIR / "images")
                        write_status(INDEX_DIR, stage="images", finished=True,
                                     documents_total=total, documents_done=total,
                                     percent=100.0, elapsed_seconds=round(time.time() - t0))
                        ENGINE._img_index = None # force a reload on the next search
                    else:
                        import os
                        os.environ["TRANSCRIBE_MEDIA"] = "1"
                        from .cli import cmd_index
                        # Re-index with transcription on; the crawl reads the
                        # environment at extraction time.
                        class _A:
                            no_semantic = True
                            rebuild = False
                            yes = True
                        cmd_index(_A())
                    state["finished"] = True
                except Exception as exc:
                    state["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    state["running"] = False

            threading.Thread(target=_run, daemon=True).start()
            self._send(200, {"started": True, "kind": kind})
            return

        if path_q.path == "/api/deep-index":
            # Started in a thread so the request returns immediately and the interface can
            # keep polling /api/index/status for the same progress the crawl uses.
            state = ENGINE.deep_index_state
            if state.get("running"):
                self._send(200, {"started": False, "reason": "already running"})
                return
            state.update({"running": True, "error": None, "finished": False})

            def _run():
                try:
                    ENGINE.store.build_trigram()
                    state["finished"] = True
                except Exception as exc:
                    state["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    state["running"] = False

            import threading
            threading.Thread(target=_run, daemon=True).start()
            self._send(200, {"started": True})
            return

        # SAVING THE MODEL SETTINGS BELONGS HERE, NOT IN do_GET.
        #
        # The first version put the read and the write in the same `if url.path == "/api/llm"`
        # block, which lived inside do_GET. So GET worked and POST returned "no such route" —
        # measured. **A handler is per-method; a route that answers both is two routes.**
        if path_q.path == "/api/llm":
            try:
                body = json.loads(self._read_body() or "{}")
            except Exception as e:
                self._send(400, {"error": f"bad JSON: {e}"})
                return
            given = (body.get("api_key") or "").strip()
            cur = llm_settings()
            # A LONE "-" MEANS REMOVE. An empty field means KEEP — a settings form that wipes a
            # secret whenever the user edits the model name is a form that loses secrets, and a
            # secret with no way out is one people regret saving.
            if given == "-":
                save_llm_settings({"api_key": "", "base_url": "", "model": ""})
                self._send(200, {"saved": True, "configured": False})
                return
            if not given and not cur.get("api_key"):
                self._send(400, {"error": "no API key supplied"})
                return
            save_llm_settings({
                "api_key": given or cur.get("api_key", ""),
                "base_url": (body.get("base_url") or cur.get("base_url")
                             or "https://api.openai.com/v1").strip(),
                "model": (body.get("model") or cur.get("model") or "gpt-4o-mini").strip(),
            })
            self._send(200, {"saved": True, "configured": True})
            return

        if path_q.path == "/api/roots":
            # SET THE ROOTS, THEN INDEX — two calls, deliberately. Setting roots is instant and
            # reversible; indexing is minutes to hours. Fusing them would make a mis-typed path
            # start a long job that cannot be stopped without killing the app.
            try:
                body = json.loads(self._read_body() or "{}")
            except Exception as e:
                self._send(400, {"error": f"bad JSON: {e}"})
                return
            roots = [str(r) for r in (body.get("roots") or [])]
            mode = body.get("mode") or "explicit"
            # "EVERYTHING" IS A MODE, NOT A LIST OF PATHS, AND THE LIST MUST BE DERIVED.
            #
            # The setup screen sends `mode: "everything"` with an empty roots list, because the
            # user is not choosing folders — they are choosing to index all of them. If the mode
            # were stored without expanding it, `ds index` would read `roots: []` and crawl
            # NOTHING, and the app would report a successful index of zero files.
            #
            # A mode that stores an empty root list is a silent no-op, which is the same
            # failure shape as every other one in this project.
            if mode and mode != "explicit":
                from .config import MODES
                m = MODES.get(mode)
                if m is None:
                    self._send(400, {"error": f"unknown mode {mode!r}",
                                     "known": sorted(MODES.keys())})
                    return
                roots = [str(r) for r in m.roots]
            missing = [r for r in roots if not Path(r).expanduser().is_dir()]
            if missing:
                # REJECTED, NOT SILENTLY SKIPPED. A typo'd path that is quietly ignored
                # produces an empty index and a user who concludes the tool is broken.
                self._send(400, {"error": "not a directory", "paths": missing})
                return
            ENGINE.store.set_meta("roots", roots)
            ENGINE.store.set_meta("mode", mode)
            self._send(200, {"roots": roots, "count": len(roots), "mode": mode,
                             "next": "POST /api/index to start"})
        elif path_q.path == "/api/roots/remove":
            try:
                body = json.loads(self._read_body() or "{}")
            except Exception as e:
                self._send(400, {"error": f"bad JSON: {e}"})
                return
            targets = [str(r) for r in (body.get("roots") or [])]
            # DRY RUN BY DEFAULT. This deletes rows, and the difference between "show me what
            # this would remove" and "remove it" should be an explicit opt-in, not a default.
            result = ENGINE.store.remove_under_roots(targets, dry_run=bool(body.get("dry_run", True)))
            if not body.get("dry_run"):
                # Dropping documents invalidates every shard that contained them, and the
                # fingerprint check detects that on the next run — no special code path needed.
                ENGINE.store.prune_missing()
                result["note"] = "run an index to rebuild the vectors for the remaining documents"
            self._send(200, result)
        elif path_q.path == "/api/install":
            # RETURNS 202 AND POLLS, because a pip install takes minutes and a blocked
            # request would look like a hang. AND STARTING TWICE IS REFUSED, not queued —
            # two pips writing the same environment is a way to corrupt it, and a double-click
            # in a UI is all it takes.
            from .runtime import INSTALLER
            if not INSTALLER.start(["sentence-transformers"]):
                self._send(409, {"error": "an install is already running",
                                 "state": INSTALLER.snapshot()})
                return
            self._send(202, {"accepted": True, "note": "installing in the background",
                             "poll": "/api/install/status"})
        elif urlparse(self.path).path == "/api/index":
            # THE BUTTON MUST DO WHAT THE COMMAND DOES, AND IT DID NOT.
            #
            # This used to run its own inline crawl: walk the roots, store the text, prune.
            # Measured against what `ds index` does, it was MISSING:
            # - the code graph (18,777 symbols, 58,475 edges)
            # - every vector (semantic search simply did not exist)
            # - the embedding policy (the 71% cost reduction)
            #
            # So the setup screen produced a text-only index and the app looked like it had
            # no semantic search — while the CLI produced a complete one. TWO INDEXING PATHS
            # THAT DISAGREE is the same class of bug as the two staleness checks that disagreed,
            # and the fix is the same: one implementation, called from both places.
            #
            # IT SPAWNS THE CLI RATHER THAN REIMPLEMENTING IT. The daemon supervises; the
            # CLI does the work. Progress arrives through index.status.json, so the UI reports a
            # button-started run and a terminal run identically.
            from .vectors import read_status
            if read_status(INDEX_DIR).get("running"):
                self._send(409, {"error": "an index run is already in progress",
                                 "state": read_status(INDEX_DIR)})
                return
            roots = ENGINE.store.get_meta("roots") or []
            if not roots:
                # REFUSED WITH A REASON. Silently indexing nothing would produce an empty
                # index and a user who concludes the app is broken.
                self._send(400, {"error": "no folders are set",
                                 "next": "POST /api/roots first"})
                return
            log_path = DB_PATH.parent / "daemon.log"
            try:
                out = open(log_path, "a")
            except OSError:
                out = subprocess.DEVNULL
            # `-u` so the log is written immediately rather than sitting in a buffer.
            cmd = index_command()
            try:
                proc = subprocess.Popen(cmd, stdout=out, stderr=out,
                                        cwd=str(Path(__file__).resolve().parent.parent))
            except Exception as e:
                self._send(500, {"error": f"could not start indexing: {e}"})
                return
            # HANDED TO THE SAME SUPERVISION auto-repair uses, so a crash is noticed and
            # retried instead of leaving the banner claiming progress forever.
            ENGINE._repair_proc = proc
            ENGINE.last_index = {"running": True, "started": time.time(), "pid": proc.pid}
            self._send(202, {"accepted": True, "pid": proc.pid,
                             "note": "indexing in the background: text, code graph and vectors",
                             "poll": "/api/index/status"})
        else:
            self._send(404, {"error": "no such route"})


def _write_token() -> str:
    """0600, AND REGENERATED PER START.

    A token that persists across restarts is a token that ends up in a config file, a backup or
    a screenshot. Regenerating costs nothing — the UI is launched by the same daemon that wrote
    it — and it means a leaked token is worthless the next time the app starts.
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(tok)
    os.chmod(TOKEN_PATH, 0o600)
    return tok


def serve(port: int = DEFAULT_PORT, open_ui: bool = True) -> None:
    global ENGINE
    ENGINE = Engine()
    ENGINE.warm()
    token = _write_token()

    # THE TIMER LIVES WITH THE SERVER, NOT WITH A REQUEST. Nothing else would run while the
    # daemon is idle, and idle is exactly when a repair should happen.
    def _repair_loop():
        # CHECK AT STARTUP, DO NOT SLEEP FIRST.
        #
        # This loop used to sleep REPAIR_CHECK_EVERY (300s) before its first look. So a user
        # who opened the app with a stale or policy-outdated index saw the warning sit there
        # for five minutes before anything noticed — and on a first run after an upgrade,
        # "the app is aware and doing nothing" is indistinguishable from "the app is broken".
        #
        # AND THE FIX FOR A SHIPPED APP IS NOT TO TELL THE USER TO RUN A TERMINAL COMMAND.
        # The whole point of auto-repair is that the app repairs itself; making the first check
        # wait five minutes means the documentation has to say "or just wait", which is worse
        # than saying nothing.
        time.sleep(REPAIR_FIRST_CHECK)
        while True:
            try:
                r = ENGINE.maybe_repair()
                if r.get("started"):
                    print(f" auto-repair started at startup: {r.get('why')}")
            except Exception as e:
                print(f" startup repair check failed: {type(e).__name__}: {e}")
            break
        while True:
            time.sleep(REPAIR_CHECK_EVERY)
            try:
                r = ENGINE.maybe_repair()
                if r.get("started"):
                    print(f" auto-repair started: {r.get('why')}")
            except Exception as e:
                # SWALLOWED ON PURPOSE: a failing repair must not kill the loop, or one bad
                # cycle would disable auto-repair for the rest of the session.
                print(f" auto-repair check failed: {type(e).__name__}: {e}")
    threading.Thread(target=_repair_loop, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.token = token
    print(f" device-search daemon")
    print(f" http://127.0.0.1:{port}")
    print(f" index : {DB_PATH} ({ENGINE.store.count():,} documents)")
    print(f" token : {TOKEN_PATH} (0600, regenerated each start)")
    print(f" semantic: {ENGINE.semantic_note or 'off'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n stopped")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="device-search local API daemon")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    serve(a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
