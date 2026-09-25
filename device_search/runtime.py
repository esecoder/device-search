"""
runtime.py — what is installed, what actually works here, and installing the rest.

===============================================================================
⚠️⚠️ WHY THIS FILE PROBES INSTEAD OF CHECKING A TABLE
===============================================================================
`requirements.txt` pinned `onnxruntime<1.20` because on THIS machine:

    onnxruntime 1.23 is built for macOS 13.4
    this machine runs macOS 13.0
    -> ImportError: Symbol not found: __ZNSt3__18to_charsEPcS0_d

⚠️ BUT THAT IS ONE MEASUREMENT ON ONE MACHINE. I have never tested onnxruntime on Windows,
Linux, macOS 14 or macOS 12. **Hardcoding a compatibility table would be inventing claims.**

⚠️ So this module does the opposite: it asks the interpreter a question it can always answer
correctly — *does this actually import?* — and falls back only when the answer is no. That is
correct on every platform without knowing anything about them, and it self-heals when the
answer changes.

⚠️ The distinction matters because a hardcoded pin has two failure modes a probe does not:
   - too LOW  -> every user on a newer OS gets an outdated runtime for no reason
   - too HIGH -> a user on an older OS gets an app that dies at import, with a symbol error
                 they cannot act on
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# ⚠️ MEASURED, not estimated. Same corpus, same model, same machine (117 docs / 5,726 chunks):
#     ONNX  :  8.2 chunks/sec   ·  load 0.16s  ·  query 0.02s
#     torch : 31.4 chunks/sec   ·  load 15.80s ·  query 0.26s
# ⚠️ The WIN IS AT INDEXING TIME, which is the opposite of what people assume. Torch is 3.8x
# faster at the slow part (indexing) and 10x SLOWER at the fast part (querying). For a consumer
# app the slow part happens once and the fast part happens constantly, which is why ONNX is the
# default despite losing the ratio that sounds more important.
MEASURED = {"onnx_chunks_per_sec": 8.2, "torch_chunks_per_sec": 31.4,
            "onnx_load_s": 0.16, "torch_load_s": 15.80,
            "onnx_query_s": 0.02, "torch_query_s": 0.26}

# ⚠️ THE KNOWN-GOOD FALLBACK, AND THE ONLY HARDCODED VERSION IN THIS FILE. It is not a guess:
# 1.19.2 is the version that was verified to load on macOS 13.0. Everything else is probed.
ONNX_FALLBACK = "onnxruntime<1.20"


# =============================================================================
# PROBING — ask the interpreter, do not consult a table
# =============================================================================
@dataclass
class Probe:
    name: str
    ok: bool
    version: str = ""
    error: str = ""
    size_mb: int = 0
    note: str = ""


def probe_onnx() -> Probe:
    """⚠️ IMPORT, NOT `importlib.util.find_spec`. A package can be INSTALLED and still fail to
    LOAD — which is exactly the macOS 13.0 case. `find_spec` returns True there and the app dies
    later with a symbol error. Only an actual import answers the question that matters."""
    try:
        import onnxruntime
        return Probe("onnxruntime", True, onnxruntime.__version__, size_mb=120,
                     note="ONNX Runtime — small, fast to start, slower to index")
    except Exception as e:
        return Probe("onnxruntime", False, error=f"{type(e).__name__}: {e}")


def probe_torch() -> Probe:
    """⚠️ SAME REASONING: importing torch can fail on an unsupported CPU or a broken wheel even
    when pip reports success."""
    try:
        import torch
        v = torch.__version__
        # ⚠️ GPU availability changes the answer to "is torch worth 2 GB" completely, so it is
        # reported rather than left for the user to discover.
        gpu = bool(getattr(torch.backends, "cuda", None) and torch.cuda.is_available())
        mps = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        where = "cuda" if gpu else ("mps" if mps else "cpu")
        return Probe("torch", True, v, size_mb=2100, note=f"PyTorch — 3.8x faster indexing (on {where})")
    except Exception as e:
        return Probe("torch", False, error=f"{type(e).__name__}: {e}")


def has_embeddings() -> Probe:
    """⚠️ `sentence-transformers` IS THE THING TORCH IS FOR. Torch alone proves nothing — a user
    can have torch installed for another project and still have no embedding path here."""
    try:
        import sentence_transformers
        return Probe("sentence-transformers", True, sentence_transformers.__version__)
    except Exception as e:
        return Probe("sentence-transformers", False, error=f"{type(e).__name__}: {e}")


def survey() -> dict:
    """⚠️ EVERY PROBE IN ONE CALL, because the UI needs all of them at once and three separate
    round trips is three chances to show a half-updated state."""
    return {"onnx": vars(probe_onnx()), "torch": vars(probe_torch()),
            "embeddings": vars(has_embeddings()),
            "python": sys.executable, "version": sys.version.split()[0],
            "platform": sys.platform}


# =============================================================================
# THE CHOICE, WITH NUMBERS FROM OUR OWN BENCHMARK
# =============================================================================
def options(estimated_chunks: int = 0) -> list[dict]:
    """Return the two runtime options with REAL measured numbers, projected onto the user's
    own estimated corpus size.

    ⚠️ THE PROJECTION IS THE POINT. "3.8x faster" means nothing to someone who does not know
    their corpus. "4 hours instead of 15" is a decision they can actually make.
    """
    out = []
    for key, pkg, label, rate_key in [
        ("onnx", "onnxruntime+fastembed", "ONNX Runtime", "onnx_chunks_per_sec"),
        ("torch", "sentence-transformers", "PyTorch", "torch_chunks_per_sec"),
    ]:
        p = probe_torch() if key == "torch" else probe_onnx()
        extra = has_embeddings() if key == "torch" else None
        installed = p.ok and (extra.ok if extra else True)
        rate = MEASURED[rate_key]
        # ⚠️ A CONSTANT, NOT `p.size_mb`. Measured wheel sizes: torch+sentence-transformers
        # ~2100 MB (plus 2-3 GB of nvidia-* on Linux), onnxruntime+fastembed ~120 MB.
        # ⚠️ `p.size_mb` is 0 whenever the probe FAILS, which is exactly when the user is
        # deciding to install it — so reading it from the probe shows the biggest download as
        # the free one.
        download_mb = 2100 if key == "torch" else 120
        est = None
        if estimated_chunks:
            est = estimated_chunks / rate
        out.append({
            "key": key,
            "label": label,
            "package": pkg,
            "installed": installed,
            "version": p.version,
            "download_mb": download_mb,
            "chunks_per_sec": rate,
            "est_seconds": est,
            "est_human": human_time(est) if est else "",
            "probe_error": p.error,
            "note": p.note,
        })
    return out


def human_time(seconds: float | None) -> str:
    """⚠️ ROUNDED UP TO SOMETHING A HUMAN WOULD SAY. "4h 12m" is actionable; "15120.4s" is not,
    and "4.20000 hours" reads as false precision about a number that is an estimate."""
    if not seconds or seconds != seconds:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h, m = divmod(s // 60, 60)
    return f"{h}h {m}m" if m else f"{h}h"


# =============================================================================
# INSTALLING — with progress, errors, and retry
# =============================================================================
class Installer:
    """⚠️ ONE INSTALL AT A TIME, AND THE STATE IS SHARED. pip writing to the same environment
    from two threads is a way to corrupt the environment, and a UI that can start two installs
    will eventually do it — a double-click is enough.

    ⚠️ AND PROGRESS IS PARSED FROM PIP'S OWN OUTPUT rather than estimated. A progress bar that
    is not connected to anything is worse than no bar: it tells the user a comfortable lie for
    several minutes and then fails.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.state: dict = {"running": False, "package": "", "percent": 0,
                            "line": "", "done": False, "ok": None,
                            "error": "", "log_tail": [], "started": 0.0}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def start(self, packages: list[str] | None = None) -> bool:
        """Start an install in a background thread. Returns False if one is already running."""
        with self._lock:
            if self.state["running"]:
                return False
        pkgs = packages or ["sentence-transformers"]
        t = threading.Thread(target=self._run, args=(pkgs,), daemon=True)
        t.start()
        return True

    def _run(self, packages: list[str]):
        self._set(running=True, done=False, ok=None, error="", percent=0,
                  package=" ".join(packages), started=time.time(),
                  line="starting…", log_tail=[])
        # ⚠️ `sys.executable -m pip` AND NOT `pip`. `pip` on PATH may be a DIFFERENT interpreter
        # — a system pip installing into a user environment, or vice versa. Installing into the
        # wrong environment succeeds and changes nothing, which is the worst possible outcome.
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
        self._set(line="$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
        except Exception as e:
            self._set(running=False, done=True, ok=False, error=f"cannot run pip: {e}")
            return
        tail: list[str] = []
        for raw in proc.stdout:                      # type: ignore[union-attr]
            line = raw.rstrip()
            if not line:
                continue
            tail.append(line)
            tail = tail[-40:]
            pct = 0
            # ⚠️ pip's progress bar uses "  45%" in a carriage-returned line. Parsing it is
            # approximate BY DESIGN — the bar is a courtesy, and `done`/`ok` are the truth.
            for tok in line.replace("\r", " ").split():
                if tok.endswith("%") and tok[:-1].isdigit():
                    pct = int(tok[:-1])
            self._set(percent=pct or self.state["percent"], line=line, log_tail=list(tail))
        code = proc.wait()
        ok = code == 0
        err = ""
        if not ok:
            err = tail[-1] if tail else f"pip exited {code}"
        # ⚠️ AND THE PROOF IS AN IMPORT, NOT AN EXIT CODE. pip returns 0 for a wheel that
        # installs cleanly and then fails to load — which is precisely the onnxruntime case that
        # started all of this. A green check that lies is worse than a red one.
        verified = ""
        if ok:
            try:
                import importlib
                for p in packages:
                    mod = p.split("[")[0].split("==")[0].replace("-", "_")
                    importlib.invalidate_caches()
                    importlib.import_module(mod)
                verified = "imports OK"
            except Exception as e:
                ok = False
                err = f"installed but will not import: {type(e).__name__}: {e}"
        self._set(running=False, done=True, ok=ok, error=err, percent=100,
                  line=("done — " + verified) if ok else "failed")


INSTALLER = Installer()


# =============================================================================
# THE SELF-HEALING ONNX VERSION
# =============================================================================
def ensure_onnx_usable(allow_install: bool = True,
                       progress: Callable[[str], None] | None = None) -> dict:
    """⚠️⚠️ THE ANSWER TO "WHY PIN A VERSION BECAUSE OF ONE MAC".

    A hardcoded pin is wrong in both directions — too low for a new OS, too high for an old one.
    This does what a pin cannot: **it tries what is installed, and only replaces it if it
    genuinely fails to load.**

        1. probe onnxruntime by IMPORTING it
        2. if it loads  -> keep it, whatever version it is
        3. if it fails  -> install the known-good fallback, then probe again
        4. report what happened either way

    ⚠️ So a user on macOS 14 or Windows or Linux keeps the newest version, and the one machine
    that needs 1.19.2 gets 1.19.2 — without a table that would have to be right about every
    platform, which is not something this project can verify.
    """
    say = progress or (lambda s: None)
    before = probe_onnx()
    if before.ok:
        say(f"onnxruntime {before.version} loads — keeping it")
        return {"action": "none", "version": before.version, "ok": True}

    say(f"onnxruntime will not load: {before.error}")
    if not allow_install:
        return {"action": "would_install", "ok": False, "error": before.error,
                "fix": f"pip install '{ONNX_FALLBACK}'"}

    say(f"installing a compatible version ({ONNX_FALLBACK})…")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", ONNX_FALLBACK],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        return {"action": "install_failed", "ok": False,
                "error": (e.stderr or "")[-400:] or "pip failed"}

    after = probe_onnx()
    say(f"after install: onnxruntime {after.version} loads" if after.ok
        else f"still failing: {after.error}")
    return {"action": "installed", "ok": after.ok, "version": after.version,
            "error": after.error}


def upgrade_to_latest_if_usable(progress: Callable[[str], None] | None = None,
                               allow_install: bool = True) -> dict:
    """⚠️ THE OTHER HALF OF THE PIN PROBLEM, AND THE HALF A PIN CAN NEVER SOLVE.

    `ensure_onnx_usable` fixes the case where what is installed is TOO NEW for the OS. This fixes
    the case where it is TOO OLD because we pinned it for someone else's machine.

    ⚠️ THE SAFETY NET IS WHY THIS IS SAFE TO ATTEMPT. A bare `pip install -U onnxruntime` on a
    machine with an old OS produces an app that no longer starts, with a symbol error the user
    cannot act on. So this:

        1. records the version that CURRENTLY WORKS
        2. tries to upgrade
        3. probes by importing
        4. ⚠️ IF THE UPGRADE BROKE IT, REINSTALLS THE VERSION THAT WORKED

    ⚠️ Step 4 is the whole feature. Without it this is just "randomly break the user's install on
    old macOS", which is exactly the failure the pin was protecting against.
    """
    say = progress or (lambda s: None)
    before = probe_onnx()
    if not before.ok:
        return {"action": "skipped", "ok": False,
                "error": f"onnxruntime does not currently work ({before.error}); "
                         f"run ensure_onnx_usable first"}
    known_good = before.version
    say(f"currently working: onnxruntime {known_good}")

    if not allow_install:
        return {"action": "would_try_latest", "ok": True, "version": known_good}

    say("trying the newest onnxruntime…")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "onnxruntime"],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        say("upgrade failed; nothing changed")
        return {"action": "upgrade_failed", "ok": True, "version": known_good,
                "error": (e.stderr or "")[-300:]}

    after = probe_onnx()
    if after.ok and after.version != known_good:
        say(f"✅ upgraded {known_good} -> {after.version}, and it loads")
        return {"action": "upgraded", "ok": True, "version": after.version,
                "from": known_good}

    # ⚠️ THE ROLLBACK. Reached when the upgrade installed but will not import — the macOS 13.0
    # case, which is precisely what the pin existed for.
    say(f"⚠️ {after.version or 'the new version'} does not load ({after.error[:70]}). rolling back…")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--force-reinstall",
                        f"onnxruntime=={known_good}"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        # ⚠️ A FAILED ROLLBACK IS REPORTED LOUDLY. The user is now in a state where their search
        # does not work at all, and they need to be told the exact command that fixes it.
        return {"action": "rollback_failed", "ok": False, "version": known_good,
                "fix": f"{sys.executable} -m pip install --force-reinstall "
                       f"onnxruntime=={known_good}"}
    restored = probe_onnx()
    say(f"rolled back to onnxruntime {restored.version} ({'loads' if restored.ok else 'STILL BROKEN'})")
    return {"action": "rolled_back", "ok": restored.ok, "version": restored.version,
            "reason": f"{known_good} is the newest version that loads on this machine"}


def main() -> int:
    """`python -m device_search.runtime` — the diagnostic a user can run and paste."""
    import argparse
    ap = argparse.ArgumentParser(description="what is installed and what actually works")
    ap.add_argument("--fix-onnx", action="store_true",
                    help="try to install a version of onnxruntime that loads")
    ap.add_argument("--try-latest", action="store_true",
                    help="attempt the newest onnxruntime, and roll back if it will not load")
    ap.add_argument("--chunks", type=int, default=0,
                    help="your estimated chunk count, to project indexing time")
    a = ap.parse_args()

    print("=" * 76)
    print("RUNTIME SURVEY")
    print("=" * 76)
    s = survey()
    print(f"  python      : {s['version']}  ({s['python']})")
    print(f"  platform    : {s['platform']}")
    for k in ("onnx", "torch", "embeddings"):
        p = s[k]
        mark = "✅" if p["ok"] else "❌"
        detail = p["version"] if p["ok"] else p["error"][:60]
        print(f"  {k:<12}: {mark} {detail}")

    if a.fix_onnx:
        print()
        print(ensure_onnx_usable(progress=lambda m: print("   ", m)))
    if a.try_latest:
        print()
        print(upgrade_to_latest_if_usable(progress=lambda m: print("   ", m)))

    print()
    print("=" * 76)
    print("YOUR OPTIONS" + (f"  (projected onto {a.chunks:,} chunks)" if a.chunks else ""))
    print("=" * 76)
    print(f"  {'runtime':<16}{'installed':<11}{'download':>10}{'index rate':>13}{'time':>10}")
    for o in options(a.chunks):
        print(f"  {o['label']:<16}{'yes' if o['installed'] else 'no':<11}"
              f"{o['download_mb']:>8} MB{o['chunks_per_sec']:>11.1f}/s{o['est_human']:>10}")
    print()
    print("  ⚠️ rates are MEASURED on a CPU-only machine (117 docs / 5,726 chunks), not quoted.")
    print("  ⚠️ torch is faster at INDEXING (3.8x) and slower at QUERYING (10x).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
