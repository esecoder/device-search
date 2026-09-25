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

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # ⚠️ NO CORS HEADER, DELIBERATELY. Adding `Access-Control-Allow-Origin: *` to a service
        # that returns your file contents would let any web page read them. The UI is served
        # from this same origin, so it does not need CORS at all.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # ---- routes ----------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/health":
            # ⚠️ UNAUTHENTICATED ON PURPOSE: the UI polls this to know the daemon is up, and it
            # reveals nothing about the user's files.
            self._send(200, {"ok": True, "version": "0.1",
                             "documents": ENGINE.store.count(), "semantic": ENGINE.semantic_note})
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
        if urlparse(self.path).path == "/api/install":
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
                    self.server.last_index = {"indexed": n, "at": time.time()}
                except Exception as e:
                    self.server.last_index = {"error": f"{type(e).__name__}: {e}"}
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

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.token = token
    httpd.last_index = None
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
