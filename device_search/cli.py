#!/usr/bin/env python3
"""
cli.py — the `ds` command.

    ds setup            choose what to index (curated, or everything with no exclusions)
    ds index            crawl and build the index
    ds search "..."     search
    ds stats            what is in the index
    ds secrets-report   what recognised secrets got indexed, and where

⚠️ EVERY COMMAND PRINTS WHAT IT DID. A search tool whose misses are indistinguishable from
"not on disk" is worse than no tool, because it makes you stop looking.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .config import (DB_PATH, INDEX_DIR, MODES, META_PATH, VEC_PATH, ensure_index_dir,
                     find_secrets, is_text_file, redact)
from .crawl import walk
from .store import Store
from .runtime import human_time

IDS_PATH = INDEX_DIR / "vectors.ids.npy"


# =============================================================================
# setup
# =============================================================================
def cmd_setup(args) -> int:
    print("=" * 78)
    print("DEVICE-SEARCH SETUP")
    print("=" * 78)
    print("""
⚠️ A SEARCH INDEX IS A COPY OF YOUR FILES IN A SECOND LOCATION. That is the point, and it is
   also the risk: whatever you index becomes readable by anything that can read the index.
""")
    for name, mode in MODES.items():
        print(f"  {name:<12} {mode.description}")
        print(f"               roots: {', '.join(str(r) for r in mode.roots) or '(none found)'}")
    print()

    if args.roots:
        # ⚠️ EXPLICIT ROOTS OVERRIDE THE MODE ENTIRELY. This is the third option from the
        # original design — "only folders I name" — and it is also how you TEST the tool
        # without crawling a home directory.
        mode = MODES.get(args.mode or "curated")
        mode = type(mode)(name="explicit", description="explicit roots",
                          roots=[Path(r).expanduser() for r in args.roots], warns=[])
        bad = [r for r in mode.roots if not r.exists()]
        if bad:
            print(f"  ✗ these roots do not exist: {[str(b) for b in bad]}")
            return 2
        ensure_index_dir()
        store = Store(DB_PATH)
        store.set_meta("mode", "explicit")
        store.set_meta("roots", [str(r) for r in mode.roots])
        store.set_meta("include_deps", bool(args.include_deps))
        print(f"\n  ✅ mode=explicit  roots={[str(r) for r in mode.roots]}")
        print("     next:  ds index")
        return 0

    mode_name = args.mode
    if not mode_name:
        mode_name = input("  choose a mode [curated/everything]: ").strip().lower()
    if mode_name not in MODES:
        print(f"  ✗ unknown mode {mode_name!r}")
        return 2
    mode = MODES[mode_name]

    # ⚠️ THE WARNING IS THE FEATURE. `everything` is genuinely useful and genuinely dangerous,
    # and the difference between the two is whether the user read the consequence before saying
    # yes. It is printed first, and it requires typing the word — not pressing enter.
    if mode_name == "everything":
        print("\n" + "!" * 78)
        for w in mode.warns:
            print("  " + w)
        print("!" * 78)
        print("\n  The index will be written to:", INDEX_DIR)
        typed = args.yes or input("  type 'everything' to confirm: ").strip().lower()
        if typed not in ("everything", "yes", "y"):
            print("  ✗ aborted — nothing was indexed.")
            return 1

    ensure_index_dir()
    roots = [str(r) for r in mode.roots]
    store = Store(DB_PATH)
    store.set_meta("mode", mode_name)
    store.set_meta("roots", roots)
    store.set_meta("include_deps", bool(args.include_deps))
    print(f"\n  ✅ mode={mode_name}  roots={roots}")
    print(f"     next:  ds index")
    return 0


# =============================================================================
# index
# =============================================================================
def cmd_deep_index(args) -> int:
    """Build the substring index used for code-fragment queries.

    ⚠⚠️ A SEPARATE, OPTIONAL, LONG STEP, AND IT IS SEPARATE BECAUSE IT IS OPTIONAL.

    Ordinary search does not need this. It exists for one kind of query —
    `InputLayer(shape=(784,))`, a fragment with no word boundary — which the word index cannot
    answer and the LIKE scan answers in ten seconds.

    ⚠️ IT IS BIG AND SLOW: measured at 8.3 GB and more than twelve minutes for 859,569
    documents. ⚠️ SO IT IS NOT PART OF `ds index` AND IS NEVER STARTED WITHOUT BEING ASKED FOR.
    A tool that quietly spends twelve minutes and eight gigabytes on a first run has decided
    something the user should have decided.

    ⚠️ AND IT RESUMES. Killed at 650,000 of 859,569, it continues from there rather than
    starting again — which is what makes it something a person can actually run.
    """
    from .vectors import read_status, write_status
    store = Store(DB_PATH)
    total = store.count()
    print("=" * 78)
    print(f"DEEP INDEX  {total:,} documents")
    print("=" * 78)
    print("  This builds an index of every three-character sequence, so that a code fragment")
    print(f"  like  InputLayer(shape=(784,))  can be found quickly instead of by a slow scan.")
    print()
    print(f"  ⚠️ It needs roughly as much disk as the index itself, and takes minutes.")
    print(f"  ⚠️ It can be stopped at any time with Ctrl-C and resumed by running this again.")
    print()
    t0 = time.time()
    try:
        n = store.build_trigram()
    except KeyboardInterrupt:
        # ⚠️ A CLEAN INTERRUPT IS THE EXPECTED EXIT, not a failure. The progress is already on
        # disk — it is committed with the data — so saying "stopped, run this again" is true.
        print()
        print("  stopped. Run the same command again to continue from here.")
        return 130
    print()
    print(f"  ✅ {n:,} documents indexed in {time.time()-t0:.0f}s")
    print(f"  ⚠️ ready: {store.trigram_ready()}")
    return 0


def cmd_index(args) -> int:
    ensure_index_dir()

    # ⚠⚠️ ONE INDEX AT A TIME, AND THIS CHECK WAS MISSING ENTIRELY.
    #
    # ⚠️ MEASURED, AND IT COST THE USER TWO HOURS: a run started while another was going. The
    # second wrote its own status, which said "finished", while the first kept walking a home
    # directory and kept the database lock. The daemon then could not start, so there was no
    # search and no AI indicator — and the user reasonably reported it as a missing button.
    #
    # ⚠️ SO `read_status` IS ASKED BEFORE ANYTHING IS TOUCHED. It decides by PID LIVENESS, not by
    # the flag in the file, because the flag is written by whichever run wrote last and the
    # process holding the lock is a different one.
    from .vectors import read_status
    live = read_status(INDEX_DIR)
    if live.get("running"):
        pid = live.get("pid")
        print(f"  ✗ an index is already running (pid {pid}).")
        print(f"    Starting a second one would have them overwrite each other's progress and")
        print(f"    fight over the database lock — which also stops the search engine starting.")
        print(f"    Wait for it, or stop it with:  kill {pid}")
        # ⚠️ NON-ZERO, because the caller MUST be able to tell that no work started. The daemon's
        # auto-repair reads this exit code to decide whether to report a failure.
        return 3

    store = Store(DB_PATH)
    roots = [Path(p) for p in store.get_meta("roots", [])]
    if not roots:
        print("  ✗ no roots configured. Run `ds setup` first.")
        return 2
    include_deps = bool(store.get_meta("include_deps", False))

    if args.rebuild:
        print("  clearing existing index…")
        store.clear()

    print("=" * 78)
    print(f"INDEXING  mode={store.get_meta('mode')}  roots={[str(r) for r in roots]}")
    print("=" * 78)

    t0 = time.time()
    # ⚠️ HAND THE CRAWLER WHAT WE ALREADY KNOW. Without this line the check above exists in
    # `crawl.py` and never fires — a parameter that is plumbed but not passed is the same bug
    # as no parameter at all, and it would have looked like the optimisation simply "not helping".
    known = store.known_state()
    print(f"  index already knows about {len(known):,} files")
    docs = walk(roots, include_deps=include_deps, known=known)
    batch, total, indexed = [], 0, 0
    _t0_crawl = time.time()
    for doc in docs:
        batch.append(doc)
        total += 1

        # ⚠⚠️ THE CRAWL MUST REPORT ITSELF. ITS SILENCE WAS THE ENTIRE “STUCK AT 100%” BUG.
        #
        # ⚠️ Nothing wrote status between “start” and “embedding”, so for the whole of a large
        # scan the interface read the LAST value on disk — 100%, from the previous run that
        # finished. The user watched a completed bar while their home directory was walked.
        #
        # ⚠️ A BAR AT 100% SAYS “FINISHED” IN THE ONE SITUATION WHERE THE APP IS WORKING HARDEST,
        # and nothing tells the user whether it is still going or has hung.
        #
        # ⚠️ A SCAN CANNOT HAVE A PERCENTAGE. The total is not knowable until the walk ends —
        # that is what a scan IS — and inventing one is how a bar ends up lying. So the crawl
        # reports what it genuinely knows: how many it has found, how long it has been going,
        # and which phase it is in. percent=None is deliberate and the interface reads it.
        if total % 500 == 0:
            write_status(INDEX_DIR, stage="crawling", finished=False,
                         files_seen=total, documents_done=indexed,
                         elapsed_seconds=int(time.time() - _t0_crawl),
                         started=_t0_crawl, percent=None, eta_seconds=0)
        if len(batch) >= 500:
            store.add_many(batch)
            indexed += len(batch)
            batch = []
            print(f"    … {indexed:,} written", flush=True)
    # ⚠️ AND WHEN THE WALK ENDS, SAY SO. Otherwise the bar holds the crawl’s last value
    # while the store is pruned and the vector plan is built — same stale status, one phase on.
    write_status(INDEX_DIR, stage="crawling", finished=False,
                 files_seen=total, documents_done=indexed,
                 elapsed_seconds=int(time.time() - _t0_crawl),
                 started=_t0_crawl, percent=None, eta_seconds=0,
                 note="scan complete — preparing to read meaning")

    if batch:
        store.add_many(batch)
        indexed += len(batch)

    # ⚠⚠️ RECORD WHAT THIS INDEX WAS BUILT WITH, THE MOMENT THE CRAWL IS COMPLETE.
    #
    # ⚠️ AFTER THE BATCHES, NOT BEFORE. Writing the version first would mark an index as current
    # before it contained the thing the version promises — so an interrupted crawl would leave a
    # v2 index with no directories in it, and nothing would ever rebuild it, because the record
    # already said it was fine.
    from .config import INDEX_FORMAT
    store.set_meta("index_format", INDEX_FORMAT)
    store.conn.commit()

    stats = getattr(walk, "stats", None)
    print()
    if stats:
        print(stats.report())
    print(f"  wall clock      : {time.time()-t0:.1f}s")

    # ⚠️ PRUNE BEFORE EMBEDDING, not after. Deleted files would otherwise keep their vectors and
    # stay searchable forever — a deleted credential that still answers queries is precisely the
    # failure a user would never think to check for.
    pruned = store.prune_missing()
    if pruned:
        print(f"  pruned (gone)   : {pruned:,}")

    if not args.no_semantic:
        _build_vectors(store, resume=not getattr(args, "restart", False))
    store.set_meta("indexed_at", time.time())
    print(f"\n  ✅ index at {DB_PATH}  ({store.count():,} documents)")
    return 0


def _build_vectors(store: Store, resume: bool = True) -> None:
    """Embed every document that is not already embedded, in resumable shards.

    ⚠️⚠️ THE LOOP IS BATCHED SO THAT INTERRUPTION COSTS ONE BATCH, NOT EVERYTHING.
    The previous version embedded all 5,687 documents and wrote once at the end — six and a
    half hours of work that an interrupt discards entirely. See `vectors.py` for why the
    manifest is written AFTER the shard files and not before.

    ⚠️ AND IT REPORTS PROGRESS WITH A MEASURED RATE, not a spinner. Six hours of no output is
    indistinguishable from a hang, and a user who cannot tell the difference will kill a
    working job — which, before this change, meant losing all of it.
    """
    from .semantic import Semantic
    from .vectors import SHARD_DOCS, VectorStore, write_status
    from .runtime import ensure_onnx_usable

    try:
        ensure_onnx_usable(progress=lambda m: print(f"    runtime: {m}"))
    except Exception:
        pass

    sem = Semantic()
    ok, why = sem.available()
    # ⚠️ THE CODE GRAPH IS BUILT REGARDLESS OF THE SEMANTIC BACKEND. It is regex and SQLite —
    # no model, no download, no GPU — and it is the retriever that actually answers the queries
    # people type about code.
    try:
        g = store.graph()
        g.clear()
        n = 0
        batch = []
        for r in store.conn.execute("SELECT id, path, text FROM documents"):
            from .crawl import FileDoc as _FD
            batch.append(_FD(r[1], 0.0, len(r[2] or ""), "", r[2] or "", 0))
            if len(batch) >= 500:
                n += store.build_graph(batch).get("symbols", 0)
                batch = []
        if batch:
            n += store.build_graph(batch).get("symbols", 0)
        st = g.stats()
        print(f"  code graph      : {st['symbols']:,} symbols, {st['edges']:,} edges over "
              f"{st['files']:,} files ({st['resolution']:.0f}% of edges resolve to a symbol)")
    except Exception as e:
        print(f"  code graph      : unavailable ({type(e).__name__}: {e})")

    print(f"\n  semantic backend: {'ON  ' + why if ok else 'OFF ' + why}")
    if not ok:
        print("    ⚠️ meaning-based queries will not work. Install sentence-transformers, or")
        print("       pass --no-semantic to silence this.")
        return

    vs = VectorStore(INDEX_DIR, model=getattr(sem, "model_name", ""),
                     runtime=sem.runtime or "", dim=384)
    if not resume and vs.man.shards:
        n = vs.reset()
        print(f"  --restart: cleared {n} shard(s), starting from zero")

    # ⚠️ THE FINGERPRINT IS TAKEN OVER THE WHOLE INDEX, not just the todo set, because that is
    # what a later search will compare against to decide whether these vectors are still valid.
    rows = list(store.conn.execute(
        "SELECT id, path, lang, n_lines, text, mtime FROM documents ORDER BY id"))
    doc_count = len(rows)
    doc_max_id = max((r[0] for r in rows), default=0)
    doc_mtime_sum = float(sum(r[5] or 0 for r in rows))

    # ⚠️⚠️ RESUME IS DRIVEN BY CONTENT FINGERPRINTS, NOT BY DOCUMENT IDS.
    # Keying on the id alone asked "have we seen this document?" when the question that matters
    # is "do our vectors still describe its CURRENT content?". A file edited from one topic to
    # another answers yes to the first and no to the second — measured: re-indexing said
    # "nothing to do — complete" for a file whose entire contents had changed, leaving a warning
    # that no amount of re-running could clear.
    current = {r[0]: f"{r[5] or 0:.3f}:{len(r[4])}" for r in rows}
    from .comments import policy_version
    plan = vs.plan_resume(current, policy=policy_version())
    if plan.get("policy_changed"):
        print(f"  ⚠️  the embedding policy changed ({plan['policy_changed']}) — every shard is")
        print(f"      rebuilt, because the old vectors answer a question we no longer ask")
    if plan["invalid_shards"]:
        n = vs.drop_shards(plan["invalid_shards"])
        # ⚠️ SAID OUT LOUD, because re-embedding neighbours costs time the user did not ask to
        # spend and would otherwise look like the resume not working.
        print(f"  ⚠️  {n} shard(s) contained changed or unverifiable documents — dropped "
              f"and queued for re-embedding")
    todo = [r for r in rows if r[0] in set(plan["todo"])]
    already = len(rows) - len(todo)
    if already:
        print(f"  resuming: {already:,} of {doc_count:,} documents already embedded "
              f"({vs.total_chunks():,} chunks in {len(vs.man.shards)} shard(s))")
    if not todo:
        vs.finish(doc_count, doc_max_id, doc_mtime_sum)
        print(f"  ✅ nothing to do — {vs.total_chunks():,} chunks, complete")
        return

    # ⚠️⚠️ ESTIMATED FROM TEXT VOLUME, NOT FROM DOCUMENT COUNT.
    #
    # The first version used `chunks_per_document = 34`, measured on a repo of long markdown
    # files. On a real corpus of 5,573 Documents/Desktop/Downloads files it is 8.2 —
    # **a 4.1x error, which turned a 1h 21m job into a 6h 33m estimate.** The progress bar was
    # then wrong in the same direction for the whole run, and at 100% it still claimed
    # "ETA 4h 10m", which is self-evidently absurd and should have been the tell.
    #
    # ⚠️ `chunks per document` is NOT a property of the chunker. It is a property of how big the
    # documents are. `bytes per chunk` IS a property of the chunker (1102 and 1357 on the two
    # corpora — 1.2x apart, against 4.1x for chunks/doc).
    #
    # ⚠️ AND WE CAN DO BETTER THAN A CONSTANT: the text phase has ALREADY finished, so the exact
    # total is sitting in SQLite. Measured on completed shards where possible.
    text_bytes = sum(len(r[4] or "") for r in todo)
    done_bytes = sum(len(r[4] or "") for r in rows if r[0] not in set(plan["todo"]))
    done_chunks = vs.total_chunks()
    if done_chunks > 500 and done_bytes > 0:
        # ⚠️ The observed ratio from THIS corpus beats any constant from another one.
        bpc = done_bytes / done_chunks
        basis = f"measured on {done_chunks:,} chunks already embedded here"
    else:
        bpc = 1200.0
        basis = "default 1200 bytes/chunk"
    est_chunks = done_chunks + max(1, int(text_bytes / bpc))
    print(f"  embedding {len(todo):,} documents "
          f"(~{est_chunks - done_chunks:,} chunks, project total {est_chunks:,}; "
          f"{bpc:.0f} bytes/chunk, {basis})")
    print(f"  in {SHARD_DOCS}-document shards")
    print(f"  ⚠️ interrupt any time — completed shards are kept and this resumes here")

    from .runtime import MEASURED
    rate = MEASURED.get("torch_chunks_per_sec" if sem.runtime == "torch"
                        else "onnx_chunks_per_sec", 8.2)
    t_start = time.time()
    t_last, chunks_last = t_start, vs.total_chunks()
    # ⚠️ ANNOUNCE THE RUN so the desktop app can show progress for a job it did not start.
    # ⚠️ AND CLEAR IT IN A `finally` at the end of this function — a crashed run must not leave
    # the UI reporting progress for ever. The reader also checks the pid, so even a SIGKILL is
    # handled; this is belt and braces because both failures are cheap to prevent.
    write_status(INDEX_DIR, stage="embedding", running=True, finished=False,
                 documents_total=doc_count, documents_done=already,
                 chunks_done=vs.total_chunks(), est_chunks=est_chunks,
                 started=t_start, shards=len(vs.man.shards))

    for i in range(0, len(todo), SHARD_DOCS):
        batch = todo[i:i + SHARD_DOCS]
        from .crawl import FileDoc
        from .comments import embed_target
        # ⚠️⚠️ THE POLICY IS APPLIED HERE, AT EMBEDDING TIME, NOT AT CRAWL TIME.
        #
        # The document keeps its FULL text — that is what exact and keyword search match
        # against, and it is never reduced. Only the EMBEDDING TARGET is narrowed: comments and
        # docstrings for code, nothing for JSON or logs, the whole file for prose.
        #
        # ⚠️ MEASURED ON THIS CORPUS: 45,811 chunks -> 13,336, and 81 minutes -> 24. Every
        # skipped file stays completely searchable by exact, keyword and filename — and code
        # additionally gains the graph, which is the retriever that answers questions ABOUT it.
        docs, targets, skipped = [], [], []
        for r in batch:
            full = r[4] or ""
            target, why = embed_target(r[1], full)
            if not target.strip():
                skipped.append((r[1], why))
                continue
            docs.append(FileDoc(r[1], r[5] or 0.0, len(full), r[2], target, r[3]))
            targets.append((r[0], target))
        if not docs:
            continue
        ids = [r[0] for r in batch if r[0] in {t[0] for t in targets}]

        t0 = time.time()
        sem.build(iter(docs))
        vecs = sem.vectors if sem.vectors is not None else None
        if vecs is None or vecs.shape[0] == 0:
            print(f"    ⚠️ shard {i // SHARD_DOCS} produced no vectors — skipping")
            continue
        per_chunk = []
        for d in docs:
            per_chunk.extend([0] * len(sem.chunk(d.text)))
        # ⚠️ The vector builder needs one id PER CHUNK, in the order chunks were emitted. Getting
        # this alignment wrong silently associates every vector with the wrong file.
        per_chunk = per_chunk[:vecs.shape[0]]
        k = 0
        for doc_id, d in zip(ids, docs):
            n = len(sem.chunk(d.text))
            for j in range(n):
                if k < len(per_chunk):
                    per_chunk[k] = doc_id
                    k += 1
        # ⚠️ BOTH LISTS, AND THEY ARE NOT THE SAME LIST. `ids` is one entry per DOCUMENT;
        # `per_chunk` is one entry per VECTOR. Passing only the second made the manifest count
        # 4,780 "documents" for a 117-document corpus.
        vs.add_shard(ids, per_chunk, vecs, seconds=time.time() - t0,
                     fingerprints=[current[i] for i in ids])

        # ---- progress with a MEASURED rate --------------------------------------
        now = time.time()
        done_docs = already + min(i + SHARD_DOCS, len(todo))
        pct = done_docs / doc_count * 100 if doc_count else 100
        span = now - t_last
        grew = vs.total_chunks() - chunks_last
        # ⚠️ A ROLLING rate, not the average. The average carries the first shard's model-load
        # cost forever, so the estimate stays pessimistic for the whole run and the user watches
        # a number that is wrong in one direction.
        if span >= 1 and grew > 0:
            rate = grew / span
            t_last, chunks_last = now, vs.total_chunks()
        remaining = max(0, est_chunks - vs.total_chunks())
        # ⚠️ NO ETA WHEN THERE IS NOTHING LEFT. The old code printed "ETA 4h 10m" on the final
        # line of a finished run, because the estimate exceeded reality and the subtraction
        # never reached zero. A completion line that says four hours remain is a warning sign
        # about the estimate, not about the run.
        eta = (remaining / rate) if (rate > 0 and pct < 99.9) else 0
        bar_n = 24
        filled = int(bar_n * pct / 100)
        write_status(INDEX_DIR, stage="embedding", finished=False,
                     documents_total=doc_count, documents_done=done_docs,
                     chunks_done=vs.total_chunks(), est_chunks=est_chunks,
                     percent=round(pct, 1), rate=round(rate, 2),
                     eta_seconds=int(eta), started=t_start, shards=len(vs.man.shards))
        print(f"    [{'█' * filled}{'·' * (bar_n - filled)}] {pct:5.1f}%  "
              f"{done_docs:,}/{doc_count:,} docs  {vs.total_chunks():,} chunks  "
              f"{rate:.1f} ch/s"
              + (f"  ETA {human_time(eta)}" if eta > 0 else "  done"), flush=True)

    vs.finish(doc_count, doc_max_id, doc_mtime_sum)
    write_status(INDEX_DIR, stage="done", running=False, finished=True,
                 documents_total=doc_count, documents_done=doc_count,
                 chunks_done=vs.total_chunks(), percent=100.0, shards=len(vs.man.shards))
    took = time.time() - t_start
    print(f"  ✅ {vs.total_chunks():,} chunks in {len(vs.man.shards)} shards "
          f"({human_time(took)} this run)")


def cmd_search(args) -> int:
    from .agent import search
    store = Store(DB_PATH)
    if store.count() == 0:
        print("  ✗ index is empty. Run `ds setup` then `ds index`.")
        return 2

    sem = None
    if not args.no_semantic:
        from .semantic import Semantic
        sem = Semantic()
        ok, why = sem.available()
        # ⚠️ THIS LINE USED TO BE `sem.vectors and None` — a leftover no-op I wrote while
        # drafting. It does nothing, LOOKS like it does something, and raises
        # `ValueError: truth value of an array is ambiguous` on a real vector matrix. So it
        # crashed EVERY semantic search while reading as deliberate. Removed.
        if not (ok and sem.load(VEC_PATH, IDS_PATH)):
            sem = None
    if sem is None and not args.no_semantic:
        # ⚠️ Only warn when it was ASKED for and unavailable.
        print("  ⚠️ semantic backend unavailable — using exact + keyword only\n")

    t0 = time.time()
    cands, trace = search(args.query, store, semantic=sem, use_llm=args.llm,
                          top_k=args.k, explain=args.explain)
    dt = time.time() - t0

    # ⚠️ RE-RANKING RUNS AFTER RETRIEVAL AND BEFORE DISPLAY. It reorders; it CANNOT add a result
    # retrieval missed, and saying so up front prevents the obvious misreading of the feature.
    if getattr(args, "rerank", False) and cands:
        from .answer import MAX_CHARS_PER_SOURCE
        from .rerank import DEFAULT_TOP_N, Reranker, rerank_candidates
        _rr = Reranker()
        _snips = {}
        for _c in cands[:DEFAULT_TOP_N]:
            _row = store.by_id(_c.doc_id)
            _snips[_c.doc_id] = ((_row[4] if _row else "") or "")[:MAX_CHARS_PER_SOURCE]
        _rep = rerank_candidates(args.query, cands, _snips, _rr)
        if _rep.get("error"):
            print(f"  re-ranking skipped: {_rep['error']}")
        else:
            print(f"  re-ranked {_rep['top_n']} candidates in {_rep['seconds']:.1f}s "
                  f"({_rep['moved']} changed position, "
                  f"{_rep.get('load_seconds', 0):.1f}s loading the model)")

    if args.json:
        print(json.dumps({
            "query": args.query, "took_s": round(dt, 3), "trace": trace,
            "results": [{"path": c.path, "line": c.line_no, "snippet": c.snippet,
                         "sources": c.sources, "score": round(c.score, 5)} for c in cands],
        }, indent=2))
        return 0

    print("=" * 78)
    print(f"QUERY  {args.query!r}")
    print("=" * 78)
    plan = trace["plan"]
    print(f"  classified as : {plan['kind']}")
    for r in plan["reasons"]:
        print(f"    · {r}")
    print(f"  ran backends  : {', '.join(plan['backends'])}")
    if trace.get("broadened"):
        print(f"  ⚠️ found nothing -> BROADENED with: {', '.join(trace['broadened_with'])}")
    print(f"  hits per backend: " +
          ", ".join(f"{k}={v}" for k, v in trace["backend_counts"].items()))
    llm = trace.get("llm", {})
    if args.llm:
        print(f"  llm rerank    : sent {llm.get('sent',0)}, "
              f"blocked {llm.get('blocked',0)}"
              + (f"  ⚠️ BLOCKED KINDS: {llm['blocked_kinds']}" if llm.get("blocked_kinds") else ""))
        if llm.get("reason"):
            print(f"                  {llm['reason']}")
    print(f"  took          : {dt:.2f}s")
    # ⚠️ PRINTED BEFORE THE RESULTS, not after. A caveat underneath a list is read as a
    # footnote; the same sentence above it is read as a qualification of what follows.
    v = trace.get("vectors") or {}
    if v.get("stale"):
        print()
        print(f"  ⚠️  SEMANTIC RESULTS MAY BE WRONG — {v.get('reason')}")
        if v.get("severity") == "incomplete":
            print(f"      embedding stopped part-way; meaning-based hits cover only part of")
            print(f"      the index. Lexical matches below are unaffected.")
        else:
            print(f"      vectors were built against an older index. Re-run `ds index` to")
            print(f"      refresh them. Exact and keyword matches are unaffected.")
    print()

    # ⚠️⚠️ THE ANSWER IS PRINTED BEFORE THE LIST, DELIBERATELY.
    #
    # It is what was asked for, and the list is the evidence for it. Printing the list first
    # buries the answer under twenty file paths the user did not ask to read — and printing
    # the answer WITHOUT the list is worse, because then the claim has no visible support.
    #
    # ⚠️ AND EVERY FAILURE MODE IS SURFACED: no model configured, a request that failed, an
    # answer citing a source that was never sent, and an answer with no citations at all. The
    # last two are the shapes that make a generated answer untrustworthy while looking sourced.
    if getattr(args, "ask", False) and cands:
        from .answer import build_context, ask as ask_model
        _srcs, _rep = build_context(
            cands, lambda c: ((store.by_id(c.doc_id) or [None, None, None, None, ""])[4] or ""))
        if _rep["blocked"]:
            print(f"  ⚠️ {_rep['blocked']} snippet(s) withheld — matched a secret pattern: "
                  f"{', '.join(_rep['blocked_kinds'])}")
        if not _srcs:
            print("  ⚠️ nothing could be sent to a model (all snippets withheld or empty)")
        else:
            _a = ask_model(args.query, _srcs)
            print()
            if _a.get("error"):
                print(f"  ⚠️ no answer: {_a['error']}")
            else:
                print(f"  {_a['answer']}")
                if _a.get("invented_citations"):
                    print(f"  ⚠️ the model cited source(s) that were never sent: "
                          f"{_a['invented_citations']} — treat this answer as unverified")
                if _a.get("uncited"):
                    print("  ⚠️ the answer carries no citations — it is an assertion, not a result")
                for _c in _a.get("citations", []):
                    print(f"      [{_c['n']}] {_c['path']}")
            print()

    if not cands:
        print("  ✗ no results.")
        # ⚠️ THE MOST IMPORTANT MESSAGE IN THE TOOL. "No results" from a partial index is a
        # different fact from "no results" from a full one, and only the tool knows which.
        print(f"    ⚠️ this search covered {store.count():,} indexed documents. Anything not")
        print(f"       indexed is invisible here — check `ds stats` before concluding a file")
        print(f"       does not exist.")
        return 1

    # ⚠️⚠️ TWO TIERS, PRINTED SEPARATELY, BECAUSE THEY ARE DIFFERENT CLAIMS.
    # Without this split, a query for a string that does not exist returned 13 results from the
    # semantic backend and looked successful. See the long note in agent.py: the score
    # distributions overlap so completely that no threshold separates them.
    matches = [c for c in cands if c.lexical]
    closest = [c for c in cands if not c.lexical]

    home = str(Path.home())

    def show(items, start=1):
        for i, c in enumerate(items, start):
            p = c.path.replace(home, "~", 1)
            print(f"  {i:>2}. {p}" + (f":{c.line_no}" if c.line_no else ""))
            print(f"      [{c.lang}, {c.n_lines} lines, via {'+'.join(c.sources)}]")
            if c.snippet:
                print(f"      {c.snippet.strip()[:150]}")
            print()

    if matches:
        print(f"  MATCHES — your words are in these files ({len(matches)})")
        print("-" * 78)
        show(matches)
    if closest:
        print(f"  CLOSEST BY MEANING — no word match, ranked by similarity ({len(closest)})")
        print("-" * 78)
        print("  ⚠️ These are NOT evidence the thing exists. Embedding similarity has no usable")
        print("     absolute threshold (measured: a garbage query scores 0.649, a real one")
        print("     0.632), so a semantic hit means 'nearest in the cone', not 'present'.")
        print()
        show(closest, start=len(matches) + 1)

    if not matches:
        # ⚠️ THE HONEST VERDICT, AND IT IS THE WHOLE POINT OF THE TOOL. A confident wrong
        # answer is worse than an honest miss here, because the user stops looking.
        print("  ⚠️ NO LEXICAL MATCH. If you typed an exact string, it is probably not in the")
        print(f"     indexed set ({store.count():,} documents). Check `ds stats` for coverage.")
        return 1
    return 0


# =============================================================================
# stats / secrets
# =============================================================================
def cmd_runtime(args) -> int:
    """The runtime chooser: what is installed, what works, and what the trade is."""
    from .runtime import options, survey, ensure_onnx_usable, upgrade_to_latest_if_usable
    print("=" * 78)
    print("EMBEDDING RUNTIME")
    print("=" * 78)
    sv = survey()
    for k in ("onnx", "torch", "embeddings"):
        p = sv[k]
        print(f"  {k:<12}: {'✅' if p['ok'] else '❌'} {p['version'] or p['error'][:64]}")

    # ⚠️ ESTIMATE THE CHUNKS so the time column means something. A rate without the user's own
    # corpus size attached is a fact they cannot act on.
    est = args.chunks
    if not est:
        store = Store(DB_PATH)
        n = store.count()
        if n:
            est = int(n * 34)     # ⚠️ 34 chunks/doc, the measured mean on a real corpus
            print(f"\n  estimating from {n:,} indexed documents (~{est:,} chunks)")
    print()
    print(f"  {'runtime':<16}{'installed':<11}{'download':>10}{'index rate':>13}{'this corpus':>14}")
    for o in options(est):
        print(f"  {o['label']:<16}{'yes' if o['installed'] else 'no':<11}"
              f"{o['download_mb']:>8} MB{o['chunks_per_sec']:>11.1f}/s{o['est_human']:>14}")
    print()
    print("  ⚠️ rates are MEASURED here, on CPU (117 docs / 5,726 chunks) — not vendor claims.")
    print("  ⚠️ torch wins at INDEXING (3.8x) and loses at QUERYING (10x). The default is ONNX")
    print("     because indexing happens once and querying happens constantly.")
    print()
    print("  to switch:")
    print("    ./.venv/bin/python -m pip install -r requirements-torch.txt")
    print("    DEVICE_SEARCH_RUNTIME=torch ./bin/ds index")
    print()
    print("  to check the onnxruntime version is the best this machine supports:")
    print("    ./.venv/bin/python -m device_search.runtime --try-latest")
    if args.fix_onnx:
        print()
        print(ensure_onnx_usable(progress=lambda m: print("   ", m)))
    if args.try_latest:
        print()
        print(upgrade_to_latest_if_usable(progress=lambda m: print("   ", m)))
    return 0


def cmd_stats(args) -> int:
    store = Store(DB_PATH)
    s = store.stats()
    print("=" * 78)
    print("INDEX STATS")
    print("=" * 78)
    print(f"  location      : {DB_PATH}")
    print(f"  size on disk  : {DB_PATH.stat().st_size/1e6:.1f} MB" if DB_PATH.exists() else "")
    print(f"  mode          : {store.get_meta('mode')}")
    print(f"  roots         : {store.get_meta('roots')}")
    print(f"  documents     : {s['documents']:,}")
    print(f"  text stored   : {s['bytes']/1e6:.1f} MB")
    print(f"  lines         : {s['lines']:,}")
    if VEC_PATH.exists():
        print(f"  vectors       : {VEC_PATH.stat().st_size/1e6:.1f} MB")
        print(f"  semantic      : ✅ built")
    else:
        print(f"  semantic      : ❌ not built (re-run `ds index` without --no-semantic)")
    print("\n  top languages:")
    for lang, n in s["langs"]:
        print(f"    {lang:<14} {n:,}")
    return 0


def cmd_secrets_report(args) -> int:
    """⚠️ Shows what recognised secrets are IN THE INDEX, redacted.

    ⚠️ THE POINT IS NOT ALARM — it is auditability. If you ran `everything` mode, you should be
    able to see what that decision actually captured, rather than hoping. Four characters are
    shown so a key can be identified and rotated without the report itself being a leak.
    """
    store = Store(DB_PATH)
    print("=" * 78)
    print("SECRETS REPORT — recognised formats present in the index (redacted)")
    print("=" * 78)
    print("⚠️ This is a REGEX SCAN of stored text. It finds the formats in config.py and nothing")
    print("   else. ⚠️ An unlabelled password in a notes file will NOT appear here.\n")
    total, by_kind, by_file = 0, {}, []
    fields = store.conn.execute("SELECT id, path, text FROM documents")
    for doc_id, path, text in fields:
        hits = find_secrets(text, limit=50)
        if hits:
            total += len(hits)
            for k, sample in hits:
                by_kind[k] = by_kind.get(k, 0) + 1
            by_file.append((path, len(hits), hits[:3]))
    print(f"  matches: {total:,} across {len(by_file):,} files\n")
    if by_kind:
        print(f"  {'kind':<20}{'count':>8}")
        for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<20}{n:>8,}")
    print()
    for path, n, samples in by_file[:args.limit]:
        print(f"  {path}  ({n} match{'es' if n != 1 else ''})")
        for k, sample in samples:
            print(f"      {k:<18} {sample}")
    if len(by_file) > args.limit:
        print(f"\n  … and {len(by_file)-args.limit:,} more files")
    print("\n  ⚠️ If any of these are live credentials, ROTATE them — deleting the file or the")
    print("     index does not un-expose a key that has already been read by another process.")
    return 0


# =============================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ds", description="Agentic search over your own device.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  ds setup --mode curated && ds index
  ds search 'InputLayer(shape=(784,))'
  ds search 'where do I configure the embedding dimension'
  ds search 'config.py' --k 5
  ds secrets-report
""")
    ap.add_argument("--version", action="version", version=f"device-search {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="choose what to index")
    p.add_argument("--mode", choices=sorted(MODES))
    p.add_argument("--roots", nargs="+", metavar="DIR",
                   help="index exactly these folders and nothing else")
    p.add_argument("--include-deps", action="store_true",
                   help="also index node_modules / .venv (off by default: they drown results)")
    p.add_argument("--yes", action="store_true", help="skip the 'type everything' confirmation")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("index", help="crawl and build the index")
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--no-semantic", action="store_true", help="skip embeddings (fast, offline)")
    # ⚠️ OPT-IN, not the default. Rebuilding from zero should be something a user CHOOSES, not
    # something that silently happens because resume could not tell the difference.
    p.add_argument("--restart", action="store_true",
                   help="discard completed vector shards and start the embedding over")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="search the index")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--llm", action="store_true",
                   help="rerank with an LLM (sends snippets to an API; secrets are blocked)")
    p.add_argument("--no-semantic", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--explain", action="store_true")
    # ⚠️⚠️ --ask IS THE HALF OF THE RAG PIPELINE THAT DID NOT EXIST. llm_rerank reordered
    # results; nothing ever SYNTHESISED an answer from them. A flag rather than the default,
    # because finding and answering are different products and the answer is only as good as
    # the list — an answer built on results the user never saw is a claim they cannot check.
    p.add_argument("--ask", action="store_true",
                   help="answer the question from the top results (needs OPENAI_API_KEY)")
    # ⚠️ THE CROSS-ENCODER, BUILT IN rerank.py AND NEVER CONNECTED — no call site anywhere.
    # Opt-in because it is O(n) forward passes: about 8 candidates per second.
    p.add_argument("--rerank", action="store_true",
                   help="reorder results with a cross-encoder (~5s, 80MB model)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("deep-index",
                       help="build the substring index for code-fragment search (slow, large)")
    p.set_defaults(fn=cmd_deep_index)

    p = sub.add_parser("runtime", help="which embedding runtime, and what it costs")
    p.add_argument("--chunks", type=int, default=0, help="project onto this many chunks")
    p.add_argument("--fix-onnx", action="store_true")
    p.add_argument("--try-latest", action="store_true")
    p.set_defaults(fn=cmd_runtime)

    p = sub.add_parser("stats", help="what is in the index")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("secrets-report", help="recognised secrets in the index (redacted)")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_secrets_report)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
