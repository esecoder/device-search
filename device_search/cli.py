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
def cmd_index(args) -> int:
    ensure_index_dir()
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
    docs = walk(roots, include_deps=include_deps)
    batch, total, indexed = [], 0, 0
    for doc in docs:
        batch.append(doc)
        if len(batch) >= 500:
            store.add_many(batch)
            indexed += len(batch)
            batch = []
            print(f"    … {indexed:,} written", flush=True)
    if batch:
        store.add_many(batch)
        indexed += len(batch)

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
        _build_vectors(store)
    store.set_meta("indexed_at", time.time())
    print(f"\n  ✅ index at {DB_PATH}  ({store.count():,} documents)")
    return 0


def _build_vectors(store: Store) -> None:
    from .semantic import Semantic
    sem = Semantic()
    ok, why = sem.available()
    print(f"\n  semantic backend: {'ON  ' + why if ok else 'OFF ' + why}")
    if not ok:
        # ⚠️ SAID OUT LOUD. Silently skipping this makes "no results" ambiguous between
        # "not on disk" and "that backend was never built".
        print("    ⚠️ meaning-based queries will not work. Install sentence-transformers, or ")
        print("       pass --no-semantic to silence this.")
        return
    print(f"    embedding {store.count():,} documents…")
    rows = store.conn.execute("SELECT id, path, lang, n_lines, text FROM documents")
    from .crawl import FileDoc
    ids = []
    docs = []
    for doc_id, path, lang, n_lines, text in rows:
        docs.append(FileDoc(path, 0.0, len(text), lang, text, n_lines))
        ids.append(doc_id)
    # ⚠️ `doc_ids` is expanded to ONE ENTRY PER CHUNK, in the same order `build` emits chunks.
    # Getting this alignment wrong silently associates every vector with the wrong file — the
    # index would still answer, and every answer would point at the wrong path.
    sem.build(iter(docs))
    per_chunk = []
    if sem.vectors is not None and sem.vectors.shape[0]:
        start = 0
        for d, doc_id in zip(docs, ids):
            n = len(sem.chunk(d.text))
            per_chunk.extend([doc_id] * n)
            start += n
        sem.doc_ids = __import__("numpy").asarray(per_chunk[:sem.vectors.shape[0]],
                                                  dtype="int64")
    sem.save(VEC_PATH, sem.doc_ids, IDS_PATH)
    print(f"    ✅ {sem.vectors.shape[0]:,} vectors -> {VEC_PATH}")


# =============================================================================
# search
# =============================================================================
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
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="search the index")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--llm", action="store_true",
                   help="rerank with an LLM (sends snippets to an API; secrets are blocked)")
    p.add_argument("--no-semantic", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--explain", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("stats", help="what is in the index")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("secrets-report", help="recognised secrets in the index (redacted)")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_secrets_report)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
