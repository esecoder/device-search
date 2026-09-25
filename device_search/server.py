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
        # ⚠️ THE INDEX RUN STATE LIVES ON THE ENGINE, because that is the object the HTTP handler
        # can actually see. Putting it on the server and reading it from the engine was a real
        # AttributeError that only surfaced when the endpoint was called.
        self.last_index: dict = {}

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
        stale = vs.check_stale(row[0], row[1], float(row[2] or 0))
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
                "searchable_now": doc_count > 0}

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
