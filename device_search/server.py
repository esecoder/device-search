#!/usr/bin/env python3
"""
server.py — the local API the desktop UI talks to.

===============================================================================
⚠️ WHY A DAEMON AND NOT A SUBPROCESS PER QUERY
===============================================================================
Measured on this machine:

    python start + open the index   : 0.051s
    embedding backend ready         : 0.338s
    query encode, warm              : 0.064s

A search box that spawns a process per keystroke pays ~0.4s EVERY TIME. A warm daemon pays it
once. **0.064s feels instant; 0.4s feels broken** — and the same numbers decide why the model
is loaded at startup rather than lazily on first query.

===============================================================================
⚠️⚠️ THE SECURITY PROBLEM NOBODY MENTIONS ABOUT LOCAL SERVERS
===============================================================================
This endpoint returns THE CONTENTS OF THE USER'S FILES. Binding to 127.0.0.1 is NOT enough:

    * ANY process on the machine can reach a localhost port — including a random npm package
    * ANY web page the user visits can issue requests to http://127.0.0.1:PORT. The browser
      blocks READING the response without CORS, but the request still happens, and a
      **DNS-rebinding** attack makes the browser believe the attacker's domain IS localhost,
      which defeats CORS entirely and lets them read everything.

So three defences, and each one is load-bearing:

    1. BIND 127.0.0.1 ONLY     — never 0.0.0.0. This is necessary and not sufficient.
    2. A TOKEN, written 0600   — every request must carry it. A random web page cannot read
                                 the token file, so it cannot use the API even if it can reach it.
    3. HOST HEADER CHECK       — reject anything whose Host is not localhost/127.0.0.1. This is
                                 what actually stops DNS rebinding, because the rebinding trick
                                 depends on sending the attacker's hostname.

⚠️ AND THE TOKEN IS NOT IN THE URL. Query strings land in logs and shell history.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import DB_PATH, INDEX_DIR, VEC_PATH
from .store import Store

IDS_PATH = INDEX_DIR / "vectors.ids.npy"
TOKEN_PATH = INDEX_DIR / "api.token"
DEFAULT_PORT = 8734

# ⚠️ AN ALLOWLIST, NOT A WILDCARD. These are the origins the webview can actually have:
#   tauri://localhost       macOS and Linux, Tauri's custom scheme
#   http://tauri.localhost  Windows
#   http://127.0.0.1:<port> a browser pointed directly at the daemon, for debugging
# ⚠️ A wildcard would let any web page the user visits read their indexed file contents.
# ⚠️ THE POLICY NUMBERS, stated here so they are arguable rather than buried:
#   300s  how often to LOOK for drift. Cheap — a manifest read and a COUNT(*) .
#   900s  how often to ACT. A repair is minutes of CPU, so acting on every glance
#         at the disk would keep a working machine permanently busy.
REPAIR_CHECK_EVERY = 300
REPAIR_COOLDOWN = 900
# ⚠️ HOW LONG TO WAIT BEFORE RETRYING A FAILED REPAIR. Separate from the success cooldown,
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
class Engine:
    """⚠️ MODULE-LEVEL STATE IS THE POINT, NOT A SMELL. The whole reason this is a daemon is
    that loading the model is expensive; a per-request object would defeat it."""

    def __init__(self):
        self.store = Store(DB_PATH)
        self.semantic = None
        self.semantic_note = ""
        self._load_errors = []
        # ⚠️ THE INDEX RUN STATE LIVES ON THE ENGINE, because that is the object the HTTP handler
        # can actually see. Putting it on the server and reading it from the engine was a real
        # AttributeError that only surfaced when the endpoint was called.
        self.last_index: dict = {}
        # ⚠️ AUTO-REPAIR, WITH A POLICY RATHER THAN A TRIGGER.
        #
        # The machinery to repair stale vectors has existed for several commits — plan_resume
        # finds the invalid shards, drop_shards removes them, the shards are resumable so only
        # those re-embed. What was missing was anything that DECIDED to run it.
        #
        # ⚠️ AND A NAIVE TRIGGER WOULD BE WRONG. "Repair whenever anything drifted" sounds
        # obviously right and is not: a browser writing cache, an editor writing swap files, any
        # log rotating — each changes a shard, and the index would re-embed CONTINUOUSLY and
        # never settle, competing with every query for CPU.
        #
        # So the trigger is a POLICY, and every clause is load-bearing:
        #   1. never while another run is active   (two writers on one index)
        #   2. never more often than the cooldown  (a repair is minutes of CPU)
        #   3. only when something is actually stale
        #   4. and it can be switched off entirely
        self.auto_repair = os.environ.get("DEVICE_SEARCH_AUTO_REPAIR", "1") != "0"
        self._last_repair = 0.0
        self._repair_note = "not needed yet"
        # ⚠️ THE CHILD IS HELD SO ITS DEATH CAN BE SEEN. A repair that starts and dies silently
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

    def search(self, query: str, k: int = 10, use_llm: bool = False) -> dict:
        from .agent import search as agent_search
        t0 = time.time()
        cands, trace = agent_search(query, self.store, semantic=self.semantic,
                                    use_llm=use_llm, top_k=k)
        home = str(Path.home())
        return {
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
                # ⚠️ `lexical` IS THE TWO-TIER FLAG AND THE UI MUST HONOUR IT. A semantic hit is
                # not evidence the thing exists; the measured score distributions overlap so
                # completely that no threshold separates them (a garbage query scored 0.649, a
                # real one 0.632). The UI must not render the two lists identically.
                "lexical": c.lexical,
                "lang": c.lang,
                "snippet": c.snippet,
                "via": c.sources,
            } for c in cands],
        }

    def index_status(self) -> dict:
        """⚠️ WHAT THE UI NEEDS TO SAY "indexing 6% — results are incomplete".

        ⚠️ THE UI CANNOT COMPUTE THIS ITSELF. Only the daemon knows which documents are in the
        index, which are embedded, and whether the vectors still describe the documents. A
        frontend guessing at it would show a confident progress bar attached to nothing.
        """
        from .vectors import VectorStore
        from .config import INDEX_DIR
        vs = VectorStore(INDEX_DIR, dim=384)
        doc_count = self.store.count()
        # ⚠️ The same fingerprint the manifest stored, recomputed over the CURRENT index, so the
        # comparison is like-for-like.
        row = self.store.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(mtime),0) FROM documents"
        ).fetchone()
        # ⚠️⚠️ THE PER-SHARD CHECK DECIDES, NOT THE GLOBAL ONE.
        #
        # `check_stale` compares a GLOBAL fingerprint — document count, max id, sum of mtimes —
        # against what `finish()` stamped at the end of a run. `plan_resume` compares PER-SHARD
        # fingerprints: which documents each shard actually contains.
        #
        # ⚠️ AND THEY CAN DISAGREE, WHICH IS WHAT MADE THIS SO CONFUSING. Measured right now:
        #
        #     plan_resume()  -> 3 invalid shards, 750 documents to re-embed
        #     check_stale()  -> stale: False, "vectors match the index"
        #
        # Because `finish()` stamps the global fingerprint from the CURRENT store whether or not
        # the shards cover it. So a run that stopped part-way, or a document removed after the
        # shards were written, leaves the coarse check saying "fine" while 750 documents have
        # vectors that describe something else.
        #
        # ⚠️ THE COARSE CHECK IS A SUMMARY AND IT WAS BEING TRUSTED AS THE ANSWER. The per-shard
        # one is the truth, because it is the one the repair actually acts on.
        stale = vs.check_stale(row[0], row[1], float(row[2] or 0))
        try:
            # ⚠️ COLUMN INDICES MUST MATCH THE SELECT. This said r[3] for a 3-column query, so it
            # raised IndexError on every call — and the handler below reported the result as
            # "not stale", which is the worst possible presentation of "the check did not run".
            cur = {r[0]: f"{r[1] or 0:.3f}:{r[2]}" for r in self.store.conn.execute(
                "SELECT id, mtime, LENGTH(text) FROM documents")}
            plan = vs.plan_resume(cur)
            if plan["invalid_shards"]:
                stale = {"stale": True, "severity": "outdated",
                         "invalid_shards": len(plan["invalid_shards"]),
                         "documents": plan["will_reembed"],
                         "reason": (f"{len(plan['invalid_shards'])} of "
                                    f"{len(vs.man.shards)} vector shards no longer match their "
                                    f"documents ({plan['will_reembed']:,} documents)")}
        except Exception as e:
            # ⚠️⚠️ A CHECK THAT CANNOT RUN MUST NOT REPORT SUCCESS.
            #
            # This previously did `stale.setdefault("note", ...)`, which left `stale: False` in
            # place. So an IndexError in the verification produced the SAME ANSWER as a healthy
            # index — **a check that fails silently is not a check**, and this one quietly
            # reported "vectors match the index" while 750 documents had wrong vectors.
            #
            # ⚠️ UNKNOWN IS NOW ITS OWN ANSWER, and it is treated as stale because the cost of
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
        # ⚠️ THE TERMINAL RUN IS THE AUTHORITY WHEN THERE IS ONE. `ds index` in a shell and
        # `POST /api/index` both write the same status file, so the UI reports either of them
        # identically — including the ETA, which the daemon could never compute on its own.
        from .vectors import read_status
        live = read_status(INDEX_DIR)
        return {"indexing": bool(live.get("running")) or run.get("running", False),
                "live": live, "run": run, "embedding": emb, "stale": stale,
                "searchable_now": doc_count > 0,
                # ⚠️ THE USER IS TOLD WHAT THE APP DOES ON ITS OWN. A background job that starts
                # itself and is not surfaced is indistinguishable from a machine that got slow.
                "auto_repair": {"enabled": self.auto_repair,
                                "last": self._last_repair, "note": self._repair_note,
                                "failures": self._repair_failures,
                                # ⚠️ So the UI can say "the repair died" instead of showing a
                                # progress bar for a process that is gone.
                                "child_alive": (self._repair_proc is not None
                                                and self._repair_proc.poll() is None)}}

    def maybe_repair(self) -> dict:
        """Spawn a repair run if the vectors have drifted. ⚠️ Called on a timer, never inline.

        ⚠️ IT SPAWNS A SEPARATE PROCESS, NOT A THREAD. Embedding is CPU-bound and would block or
        starve the request threads that serve searches. A subprocess can also be killed, resumed
        and observed — none of which is true of a thread inside the server.
        """
        if not self.auto_repair:
            return {"started": False, "reason": "auto-repair is off"}
        from .vectors import read_status
        live = read_status(INDEX_DIR)
        if live.get("running"):
            return {"started": False, "reason": "a run is already in progress"}
        # ⚠️ FIRST: DID THE LAST REPAIR DIE? Checked BEFORE the cooldown, because a repair that
        # failed must not be locked out for 15 minutes by the same timer that paces successful
        # ones. ⚠️ And the failure is surfaced rather than absorbed — the banner must not go on
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
                # ⚠️ A SHORT BACKOFF AFTER FAILURE, not the full cooldown. Long enough not to
                # spin on a broken environment, short enough to recover from a one-off.
                self._last_repair = time.time() - (REPAIR_COOLDOWN - REPAIR_FAIL_BACKOFF)

        if time.time() - self._last_repair < REPAIR_COOLDOWN:
            left = int((REPAIR_COOLDOWN - (time.time() - self._last_repair)) / 60)
            return {"started": False,
                    "reason": f"cooldown, {left}m remaining"
                              + (f" — last attempt: {self._repair_note}"
                                 if self._repair_failures else "")}
        st = self.index_status()
        if not st["stale"].get("stale"):
            self._repair_note = "up to date"
            return {"started": False, "reason": "up to date"}

        log_path = DB_PATH.parent / "daemon.log"
        try:
            out = open(log_path, "a")
        except OSError:
            out = subprocess.DEVNULL
        cmd = [sys.executable, "-u", "-m", "device_search.cli", "index"]
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
        # ⚠️ HOST CHECK FIRST — this is the DNS-rebinding defence. A rebinding attack sends
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

    def log_message(self, fmt, *args):        # ⚠️ quiet: a daemon logging every poll is noise
        pass

    def _read_body(self) -> str:
        """⚠️ stdlib http.server does NOT read the body for you, and forgetting this makes every
        POST look like it arrived empty."""
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # ⚠️⚠️ CORS, AND THE COMMENT THAT WAS HERE FOR SEVERAL ROUNDS WAS WRONG.
        #
        # It said: "The UI is served from this same origin, so it does not need CORS at all."
        # ⚠️ THAT IS FALSE. The Tauri webview runs at `tauri://localhost` — a CUSTOM SCHEME,
        # not http://127.0.0.1:8734. Different origin, so CORS applies, and omitting the headers
        # blocked the app's OWN interface while curl (which has no origin and no CORS) kept
        # working perfectly. That is why every terminal test I ran said the daemon was healthy.
        #
        # ⚠️ AND `X-DS-Token` IS WHY IT FAILED OUTRIGHT RATHER THAN PARTIALLY. A custom header
        # is not a "simple request", so the browser MUST send an OPTIONS preflight first. There
        # was no do_OPTIONS, so it got 501 and the real request was never sent.
        #
        # ⚠️ THE ORIGIN IS AN ALLOWLIST, NOT `*`. `Access-Control-Allow-Origin: *` would let ANY
        # web page the user visits read their file contents — the exact threat the Host-header
        # check exists to stop, reopened through a different door.
        self._cors()
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # ---- routes ----------------------------------------------------------
    def _cors(self) -> None:
        """⚠️ EMITTED FROM ONE PLACE, because the preflight and the real response MUST agree.

        The first version of this fix set these headers inside `_send()` only, so `do_OPTIONS`
        returned a bare `204` with no `Access-Control-Allow-*` at all — and the browser rejects
        that exactly as it rejected the 501. A preflight that does not actually authorise the
        request it precedes is the same as no preflight.
        """
        origin = self.headers.get("Origin") or ""
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            # ⚠️ Vary matters: without it a cache can serve one origin's response to another.
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "X-DS-Token, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        """⚠️⚠️ THE PREFLIGHT, AND ITS ABSENCE WAS THE ENTIRE BUG.

        The webview sends `OPTIONS` before every request carrying `X-DS-Token`. Without this
        handler the stdlib answers **501 Unsupported method**, the browser refuses to send the
        real request, and `fetch` rejects with WebKit's unhelpful "Load failed" — which is what
        the UI displayed as "daemon unreachable (loading failed)".

        ⚠️ It is answered WITHOUT a token check on purpose: a preflight carries no credentials
        by design, and the real request is still authenticated immediately afterwards.
        """
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/health":
            # ⚠️ UNAUTHENTICATED ON PURPOSE: the UI polls this to know the daemon is up, and it
            # reveals nothing about the user's files.
            st = ENGINE.index_status()
            self._send(200, {"ok": True, "version": "0.1",
                             "documents": ENGINE.store.count(),
                             "semantic": ENGINE.semantic_note,
                             # ⚠️ THE WARNING TRAVELS ON THE CHEAP POLL. The UI polls health
                             # every few seconds; making it also fetch /api/index/status just to
                             # know whether to draw a banner would double the polling for one
                             # boolean.
                             "stale": st["stale"],
                             "embedding_percent": st["embedding"]["percent"],
                             "indexing": st["indexing"],
                             # ⚠️ The ETA and rate come from the process DOING the work. The
                             # daemon cannot derive them, so it must not invent them.
                             "live": {k: st["live"].get(k) for k in
                                      ("running", "percent", "rate", "eta_seconds",
                                       "documents_done", "documents_total", "reason")}})
            return
        if not self._authorised():
            return
        q = parse_qs(url.query)
        try:
            if url.path == "/api/search":
                query = (q.get("q") or [""])[0]
                if not query.strip():
                    self._send(400, {"error": "empty query"})
                    return
                k = min(int((q.get("k") or ["10"])[0]), 50)
                llm = (q.get("llm") or ["0"])[0] == "1"
                self._send(200, ENGINE.search(query, k=k, use_llm=llm))
            elif url.path == "/api/stats":
                self._send(200, ENGINE.stats())
            elif url.path == "/api/roots":
                from pathlib import Path as _P

                from .config import MODES
                configured = ENGINE.store.get_meta("roots") or []
                # ⚠️ SUGGESTED FOLDERS ARE CHECKED FOR EXISTENCE, so the UI never offers a
                # folder the user does not have. An empty Downloads on a fresh machine is normal.
                suggested = [
                    {"path": str(f), "name": f.name, "exists": f.is_dir()}
                    for f in (_P.home() / "Documents", _P.home() / "Desktop", _P.home() / "Downloads")
                ]
                self._send(200, {"configured": configured,
                                 "indexed": ENGINE.store.roots_in_index(),
                                 "suggested": suggested,
                                 "modes": sorted(MODES.keys()),
                                 "documents": ENGINE.store.count()})
            elif url.path == "/api/index/status":
                self._send(200, ENGINE.index_status())
            elif url.path == "/api/runtime":
                # ⚠️ THE CHOOSER, SERVED. The UI cannot run pip itself and must not try — it
                # has no idea which interpreter is running the engine, and installing into the
                # wrong one succeeds while changing nothing.
                from .runtime import options, survey
                est = 0
                n = ENGINE.store.count()
                if n:
                    est = int(n * 34)          # measured mean chunks/doc
                self._send(200, {"survey": survey(), "options": options(est),
                                 "estimated_chunks": est})
            elif url.path == "/api/install/status":
                from .runtime import INSTALLER
                self._send(200, INSTALLER.snapshot())
            else:
                self._send(404, {"error": "no such route"})
        except Exception as e:
            # ⚠️ ERRORS ARE RETURNED, NOT SWALLOWED. A UI that shows an empty list when the
            # backend threw is indistinguishable from "not found" — the exact failure this
            # project keeps recording.
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._authorised():
            return
        path_q = urlparse(self.path)
        if path_q.path == "/api/roots":
            # ⚠️ SET THE ROOTS, THEN INDEX — two calls, deliberately. Setting roots is instant and
            # reversible; indexing is minutes to hours. Fusing them would make a mis-typed path
            # start a long job that cannot be stopped without killing the app.
            try:
                body = json.loads(self._read_body() or "{}")
            except Exception as e:
                self._send(400, {"error": f"bad JSON: {e}"})
                return
            roots = [str(r) for r in (body.get("roots") or [])]
            missing = [r for r in roots if not Path(r).expanduser().is_dir()]
            if missing:
                # ⚠️ REJECTED, NOT SILENTLY SKIPPED. A typo'd path that is quietly ignored
                # produces an empty index and a user who concludes the tool is broken.
                self._send(400, {"error": "not a directory", "paths": missing})
                return
            ENGINE.store.set_meta("roots", roots)
            ENGINE.store.set_meta("mode", body.get("mode") or "explicit")
            self._send(200, {"roots": roots, "count": len(roots),
                             "next": "POST /api/index to start"})
        elif path_q.path == "/api/roots/remove":
            try:
                body = json.loads(self._read_body() or "{}")
            except Exception as e:
                self._send(400, {"error": f"bad JSON: {e}"})
                return
            targets = [str(r) for r in (body.get("roots") or [])]
            # ⚠️ DRY RUN BY DEFAULT. This deletes rows, and the difference between "show me what
            # this would remove" and "remove it" should be an explicit opt-in, not a default.
            result = ENGINE.store.remove_under_roots(targets, dry_run=bool(body.get("dry_run", True)))
            if not body.get("dry_run"):
                # ⚠️ Dropping documents invalidates every shard that contained them, and the
                # fingerprint check detects that on the next run — no special code path needed.
                ENGINE.store.prune_missing()
                result["note"] = "run an index to rebuild the vectors for the remaining documents"
            self._send(200, result)
        elif path_q.path == "/api/install":
            # ⚠️ RETURNS 202 AND POLLS, because a pip install takes minutes and a blocked
            # request would look like a hang. ⚠️ AND STARTING TWICE IS REFUSED, not queued —
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
            # ⚠️ RUNS IN A THREAD so the UI stays responsive and can poll /api/health while a
            # long media index runs. The alternative — blocking the request — makes the app
            # look frozen for the 18-36 hours a full OCR pass could take.
            def run():
                # ⚠️ THE FLAG IS SET AND CLEARED IN A `finally`, so an exception cannot leave the
                # UI showing "indexing…" forever with no way to tell that it died.
                ENGINE.last_index = {"running": True, "started": time.time()}
                try:
                    from .crawl import walk
                    from .config import MODES
                    roots = [Path(p) for p in (ENGINE.store.get_meta("roots") or [])]
                    known = ENGINE.store.known_state()
                    batch, n = [], 0
                    for doc in walk(roots, known=known):
                        batch.append(doc)
                        if len(batch) >= 200:
                            ENGINE.store.add_many(batch)
                            n += len(batch)
                            batch = []
                    if batch:
                        ENGINE.store.add_many(batch)
                        n += len(batch)
                    ENGINE.store.prune_missing()
                    ENGINE.last_index = {"running": False, "indexed": n,
                                         "at": time.time()}
                except Exception as e:
                    ENGINE.last_index = {"running": False,
                                         "error": f"{type(e).__name__}: {e}"}
            t = threading.Thread(target=run, daemon=True)
            t.start()
            self._send(202, {"accepted": True, "note": "indexing in the background"})
        else:
            self._send(404, {"error": "no such route"})


def _write_token() -> str:
    """⚠️ 0600, AND REGENERATED PER START.

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

    # ⚠️ THE TIMER LIVES WITH THE SERVER, NOT WITH A REQUEST. Nothing else would run while the
    # daemon is idle, and idle is exactly when a repair should happen.
    def _repair_loop():
        while True:
            time.sleep(REPAIR_CHECK_EVERY)
            try:
                r = ENGINE.maybe_repair()
                if r.get("started"):
                    print(f"  auto-repair started: {r.get('why')}")
            except Exception as e:
                # ⚠️ SWALLOWED ON PURPOSE: a failing repair must not kill the loop, or one bad
                # cycle would disable auto-repair for the rest of the session.
                print(f"  auto-repair check failed: {type(e).__name__}: {e}")
    threading.Thread(target=_repair_loop, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.token = token
    print(f"  device-search daemon")
    print(f"    http://127.0.0.1:{port}")
    print(f"    index  : {DB_PATH} ({ENGINE.store.count():,} documents)")
    print(f"    token  : {TOKEN_PATH} (0600, regenerated each start)")
    print(f"    semantic: {ENGINE.semantic_note or 'off'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="device-search local API daemon")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    serve(a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
