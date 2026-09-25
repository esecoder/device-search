"""
vectors.py — resumable, sharded vector storage.

===============================================================================
⚠️⚠️ THE PROBLEM THIS SOLVES, MEASURED
===============================================================================
Embedding 5,687 documents takes ~6.5 hours on ONNX. The previous implementation read every
document, embedded every chunk, and called `save()` ONCE at the end:

    sem.build(all_docs)      # 6.5 hours
    sem.save(VECTORS)        # ← everything lands on disk HERE

⚠️ **Interrupt at hour six and the result is nothing.** Worse, the stale file from a previous
run stays in place, and nothing marks it stale — so the app keeps serving semantic results from
an index that no longer matches the documents it claims to describe.

⚠️ AND IT WAS NOT EVEN A CRASH-SAFETY PROBLEM. It was a DESIGN problem: the unit of work was
"all of it", so there was nowhere to resume from.

===============================================================================
THE DESIGN: SHARDS THAT ARE INDIVIDUALLY ATOMIC
===============================================================================
    1. embed a BATCH of documents
    2. write vectors.BBBB.npy and vectors.BBBB.ids.npy
    3. ⚠️ THEN update the manifest that records the batch as done

⚠️ THE ORDER IS THE ENTIRE GUARANTEE. A crash between 2 and 3 leaves orphaned files the manifest
does not mention — wasted work, never wrong results. The reverse order would record a batch as
complete before its data existed, and resume would skip a shard that was never written.

⚠️ So resumption is not a feature that has to work correctly under interruption. **It is a
consequence of doing the writes in an order where the worst case is wasted work.**
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MANIFEST = "vectors.manifest.json"
# ⚠️⚠️ THE CROSS-PROCESS PROGRESS FILE, AND WHY IT IS NOT THE MANIFEST.
#
# The manifest records what is DONE. This records what is HAPPENING — and they are different
# questions asked by different processes. `./bin/ds index` runs in a terminal; the desktop app
# runs a daemon. The daemon can see shards appearing but cannot tell "a run is in progress"
# from "a run finished 30 seconds ago", and it has no idea of the ETA.
#
# ⚠️ A LOCKFILE WITH A PID IS THE STANDARD ANSWER, and the failure it prevents is the classic
# one: a run that is killed leaves a status file claiming "indexing, 47%", and the app reports
# progress for a process that no longer exists. So the reader CHECKS THE PID IS ALIVE rather
# than trusting the file.
STATUS = "index.status.json"
# ⚠️ A SHARD IS A DOCUMENT BATCH, NOT A SIZE. Bounding by bytes would split a document's chunks
# across two files, and then a resume has to reason about partial documents. Bounding by
# documents makes every shard self-contained.
SHARD_DOCS = 250


@dataclass
class Shard:
    file: str
    ids_file: str
    documents: int
    chunks: int
    seconds: float = 0.0


@dataclass
class Manifest:
    model: str = ""
    runtime: str = ""
    dim: int = 0
    shards: list[dict] = field(default_factory=list)
    done_doc_ids: list[int] = field(default_factory=list)
    complete: bool = False
    started: float = 0.0
    updated: float = 0.0
    # ⚠️ THE STALENESS FINGERPRINT. Recorded when the vectors are built so a later search can ask
    # "do these vectors still describe the documents in the index?" — the question the old
    # implementation could not answer at all.
    doc_count: int = 0
    doc_max_id: int = 0
    doc_mtime_sum: float = 0.0


def write_status(index_dir: Path, **fields) -> None:
    """⚠️ Written by whichever process is doing the work, read by every other one."""
    p = Path(index_dir) / STATUS
    try:
        fields["pid"] = os.getpid()
        fields["updated"] = time.time()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fields))
        os.replace(tmp, p)          # ⚠️ atomic, so a reader never sees a half-written file
        os.chmod(p, 0o600)
    except OSError:
        pass


def read_status(index_dir: Path) -> dict:
    """⚠️ RETURNS running=False FOR A DEAD PID, whatever the file says.

    A killed process cannot clean up after itself, so the file on disk is not evidence that
    anything is running. Asking the OS whether that pid still exists is.
    """
    p = Path(index_dir) / STATUS
    if not p.exists():
        return {"running": False, "reason": "no run has been started"}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {"running": False, "reason": "unreadable status file"}
    pid = d.get("pid")
    if d.get("finished"):
        d["running"] = False
        return d
    if pid:
        try:
            os.kill(pid, 0)         # ⚠️ signal 0 = "does this process exist?" and nothing else
        except (OSError, ProcessLookupError):
            d["running"] = False
            d["reason"] = f"the process that was indexing ({pid}) is gone"
            return d
    d["running"] = True
    return d


class VectorStore:
    """Sharded, resumable vectors with a manifest.

    ⚠️ ONE INSTANCE PER PROCESS IS ASSUMED, and that is safe because indexing is a single
    threaded batch job in the CLI and a single background thread in the daemon.
    """

    def __init__(self, index_dir: Path, model: str = "", runtime: str = "",
                 dim: int = 384):
        self.dir = Path(index_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / MANIFEST
        self.man = self._read_manifest()
        if model and not self.man.model:
            self.man.model = model
        if runtime and not self.man.runtime:
            self.man.runtime = runtime
        if dim and not self.man.dim:
            self.man.dim = dim
        self._vectors: np.ndarray | None = None
        self._ids: np.ndarray | None = None
        # ⚠️ HOW MANY SHARDS ARE CURRENTLY IN MEMORY, tracked separately from the manifest.
        # This is what makes an incremental refresh possible: the daemon keeps serving while the
        # CLI indexes, and picks up each new shard WITHOUT re-reading the ones it already has.
        self._loaded_shards = 0
        self._loaded_mtime = 0.0

    # ---------------------------------------------------------------- manifest
    def _read_manifest(self) -> Manifest:
        if not self.manifest_path.exists():
            return Manifest()
        try:
            d = json.loads(self.manifest_path.read_text())
            m = Manifest(**{k: v for k, v in d.items() if k in Manifest.__dataclass_fields__})
            return m
        except Exception:
            # ⚠️ A CORRUPT MANIFEST IS TREATED AS ABSENT, not fatal. The shard files are still
            # there and still correct; an unreadable bookkeeping file should cost a re-index of
            # the metadata, not the app. ⚠️ And it is MOVED ASIDE rather than deleted, because
            # silently destroying the only record of what was done is how a bug becomes
            # unreproducible.
            try:
                self.manifest_path.rename(self.manifest_path.with_suffix(".json.bad"))
            except OSError:
                pass
            return Manifest()

    def _write_manifest(self) -> None:
        self.man.updated = time.time()
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.man.__dict__, indent=2))
        # ⚠️ ATOMIC REPLACE. A plain write can be interrupted half-way and leave a truncated
        # JSON file — which would then be read on the next start and discard all progress.
        os.replace(tmp, self.manifest_path)

    # ------------------------------------------------------------------ state
    def done_doc_ids(self) -> set[int]:
        return set(self.man.done_doc_ids)

    def plan_resume(self, current: dict[int, str]) -> dict:
        """Which shards are invalid, and which documents still need embedding.

        ⚠️⚠️ THIS IS THE FIX FOR "THE WARNING HAS NO REMEDY". `done_doc_ids` answers "have we seen
        this document?", which is the wrong question. The right one is "do our vectors still
        describe this document's CURRENT CONTENT?" — and a file edited from one topic to another
        answers yes to the first and no to the second.

        ⚠️ INVALIDATION IS PER-SHARD, NOT PER-DOCUMENT. A shard is ~250 documents and is written
        as one unit, so a changed document means re-embedding its whole shard. That is
        deliberately wasteful: the alternative is surgery inside a written shard, and 250
        documents is a few seconds of work against a 6-hour run.
        """
        invalid: list[int] = []
        for i, sh in enumerate(self.man.shards):
            fps = sh.get("doc_fps") or {}
            # ⚠️ A shard written before fingerprints existed has no way to prove it is current,
            # so it is treated as invalid. Re-embedding once is the price of not silently
            # trusting data that cannot be checked.
            if not fps:
                invalid.append(i)
                continue
            for did_s, fp in fps.items():
                did = int(did_s)
                if did not in current or current[did] != fp:
                    invalid.append(i)
                    break

        surviving = {i for i in range(len(self.man.shards))} - set(invalid)
        covered: set[int] = set()
        for i in surviving:
            covered.update(int(d) for d in (self.man.shards[i].get("doc_fps") or {}))
        todo = sorted(d for d in current if d not in covered)
        return {"invalid_shards": invalid, "todo": todo,
                "kept_documents": len(covered), "total_documents": len(current),
                "will_reembed": len(todo) + sum(self.man.shards[i].get("documents", 0)
                                                for i in invalid)}

    def drop_shards(self, indices: list[int]) -> int:
        """Remove shards from the manifest AND the disk. ⚠️ Both, or the index is corrupt."""
        if not indices:
            return 0
        drop = set(indices)
        keep, removed = [], 0
        for i, sh in enumerate(self.man.shards):
            if i in drop:
                for name in (sh.get("file"), sh.get("ids_file")):
                    if name:
                        try:
                            (self.dir / name).unlink()
                        except OSError:
                            pass
                removed += 1
            else:
                keep.append(sh)
        self.man.shards = keep
        # ⚠️ done_doc_ids is DERIVED from the shards, so it is rebuilt rather than edited. Leaving
        # a stale id in it would make the next resume skip a document that is no longer covered.
        self.man.done_doc_ids = [int(d) for sh in keep for d in (sh.get("doc_fps") or {})]
        self.man.complete = False
        self._write_manifest()
        self._vectors = self._ids = None
        self._loaded_shards = 0
        return removed

    def is_complete(self) -> bool:
        return bool(self.man.complete) and bool(self.man.shards)

    def total_chunks(self) -> int:
        return sum(s.get("chunks", 0) for s in self.man.shards)

    def summary(self) -> dict:
        """⚠️ Everything the UI and the console need to say how far along we are."""
        return {
            "shards": len(self.man.shards),
            "documents_done": len(self.man.done_doc_ids),
            "chunks": self.total_chunks(),
            "complete": self.man.complete,
            "model": self.man.model,
            "runtime": self.man.runtime,
            "updated": self.man.updated,
            "stale": None,      # filled by check_stale()
        }

    # ------------------------------------------------------------------ writing
    def add_shard(self, documents: list[int], chunk_ids: list[int], vectors: np.ndarray,
                  seconds: float = 0.0, fingerprints: list[str] | None = None) -> int:
        """Append one batch. ⚠️ Files first, manifest SECOND — see the module docstring.

        ⚠️⚠️ `documents` AND `chunk_ids` ARE DIFFERENT LISTS AND CONFLATING THEM WAS A BUG I
        SHIPPED FOR ONE RUN. My first version took only the per-chunk ids — one entry per VECTOR
        — and used them as the document accounting too. The manifest then read:

            "documents": 4780      (a 117-document corpus)
            "done_doc_ids": [1,1,1,2,2,2,...]

        ⚠️ The vector alignment was still CORRECT, so search worked and nothing looked broken.
        **What broke was the bookkeeping that resumption depends on**: `documents_done` reported
        4,780, and a per-document count is what a resume and an ETA are computed from.

            documents   one id per DOCUMENT      -> done_doc_ids, progress, ETA
            chunk_ids   one id per CHUNK/VECTOR  -> which file each vector belongs to
        """
        if len(chunk_ids) != vectors.shape[0]:
            raise ValueError(f"{len(chunk_ids)} chunk ids for {vectors.shape[0]} vectors")
        if not documents:
            raise ValueError("a shard must record which documents it covers")
        # ⚠️ A SHARD WITHOUT FINGERPRINTS CANNOT BE INVALIDATED LATER, so it is refused here
        # rather than producing a shard that silently cannot be repaired.
        if fingerprints is None or len(fingerprints) != len(documents):
            raise ValueError("every embedded document needs a fingerprint")
        idx = len(self.man.shards)
        vf = self.dir / f"vectors.{idx:04d}.npy"
        idsf = self.dir / f"vectors.{idx:04d}.ids.npy"
        np.save(vf, vectors.astype(np.float32))
        # ⚠️ THE FILE GETS THE PER-CHUNK IDS. The manifest gets the per-document ones. Swapping
        # these two lines would make search point every vector at the wrong file.
        np.save(idsf, np.asarray(chunk_ids, dtype=np.int64))
        # ⚠️ 0600: these are a lossy encoding of file contents and are exactly as sensitive as
        # the index itself.
        for f in (vf, idsf):
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
        self.man.shards.append({"file": vf.name, "ids_file": idsf.name,
                                "documents": len(documents), "chunks": int(vectors.shape[0]),
                                "seconds": round(seconds, 3),
                                # ⚠️⚠️ THE FINGERPRINTS ARE WHAT MAKE STALENESS FIXABLE INSTEAD
                                # OF MERELY DETECTABLE. Without them, "already embedded" means
                                # "this doc id appears somewhere" — so a file edited from one
                                # topic to another keeps its OLD vector for ever, re-indexing
                                # says "nothing to do", and the staleness warning fires with no
                                # remedy but --restart. Measured: editing a file's entire content
                                # left `nothing to do — complete`.
                                "doc_fps": {str(d): fp for d, fp in zip(documents, fingerprints)}})
        self.man.done_doc_ids.extend(int(d) for d in documents)
        if not self.man.started:
            self.man.started = time.time()
        self._write_manifest()
        # ⚠️ THE COUNTER IS SYNCED, NOT RESET TO ZERO. This process just wrote the shard, so it
        # is already accounted for — zeroing would make the next refresh load it a second time
        # and silently duplicate every vector in the shard.
        self._loaded_shards = len(self.man.shards)
        try:
            self._loaded_mtime = self.manifest_path.stat().st_mtime
        except OSError:
            self._loaded_mtime = 0.0
        return int(vectors.shape[0])

    def finish(self, doc_count: int = 0, doc_max_id: int = 0, doc_mtime_sum: float = 0.0) -> None:
        self.man.complete = True
        self.man.doc_count = doc_count
        self.man.doc_max_id = doc_max_id
        self.man.doc_mtime_sum = round(doc_mtime_sum, 3)
        self._write_manifest()

    def reset(self) -> int:
        """Remove every shard. ⚠️ Returns how many were deleted so a caller can report it."""
        n = len(self.man.shards)
        for s in self.man.shards:
            for name in (s.get("file"), s.get("ids_file")):
                if name:
                    try:
                        (self.dir / name).unlink()
                    except OSError:
                        pass
        self.man = Manifest()
        self._write_manifest()
        self._vectors = self._ids = None
        self._loaded_shards = 0
        return n

    def prune_orphans(self) -> int:
        """⚠️ Shard files the manifest does not know about — the wreckage of an interrupted run.
        They are harmless (never read) and they accumulate, so they are cleaned up and counted."""
        known = {s.get("file") for s in self.man.shards} | \
                {s.get("ids_file") for s in self.man.shards}
        removed = 0
        for f in self.dir.glob("vectors.*.npy"):
            if f.name not in known and not f.name.startswith("vectors.manifest"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    # ------------------------------------------------------------------ reading
    def refresh(self) -> bool:
        """⚠️⚠️ PICK UP SHARDS WRITTEN BY ANOTHER PROCESS — the whole reason this exists.

        The two-terminal workflow is `./bin/ds index` in one tab and `cargo run` in another. The
        daemon loads its vectors at startup and, without this, would keep serving THAT shard set
        for as long as it ran: indexing proceeds, the manifest grows, and search silently returns
        results from a frozen snapshot. ⚠️ The staleness banner would say "incomplete" — so the
        user is warned — but the vectors on screen would never update, which is worse than a
        warning can express.

        ⚠️ INCREMENTAL, NOT A FULL RELOAD. A 250-document shard lands every ~30 seconds during a
        long index, and re-reading every shard each time is O(n^2) over the run. Appending only
        the new ones keeps each refresh proportional to what actually changed.
        """
        try:
            mt = self.manifest_path.stat().st_mtime
        except OSError:
            return self._vectors is not None
        if mt == self._loaded_mtime and self._vectors is not None:
            return True
        # ⚠️ Re-read the manifest itself: another process rewrote it, so our in-memory copy is
        # the thing that is out of date, not just the shard files.
        self.man = self._read_manifest()
        self._loaded_mtime = mt

        if len(self.man.shards) <= self._loaded_shards:
            return self._vectors is not None

        vecs, ids = [], []
        for s in self.man.shards[self._loaded_shards:]:
            vf = self.dir / s["file"]
            if not vf.exists():
                # ⚠️ A HALF-WRITTEN SHARD IS EXPECTED HERE, not an anomaly. The CLI writes the
                # .npy and the .ids.npy and only THEN updates the manifest, so a shard named in
                # the manifest is complete by construction. If it is missing anyway something
                # deleted it, and skipping it would quietly drop a batch of the corpus.
                raise FileNotFoundError(f"shard listed in the manifest is gone: {vf}")
            vecs.append(np.load(vf))
            ids.append(np.load(self.dir / s["ids_file"]))
        if not vecs:
            return self._vectors is not None

        newv = np.concatenate(vecs, axis=0)
        newi = np.concatenate(ids, axis=0)
        if self._vectors is None:
            self._vectors, self._ids = newv, newi
        else:
            self._vectors = np.concatenate([self._vectors, newv], axis=0)
            self._ids = np.concatenate([self._ids, newi], axis=0)
        self._loaded_shards = len(self.man.shards)
        return self._vectors.shape[0] > 0

    def load(self) -> bool:
        return self.refresh()

    def search(self, qvec: np.ndarray, limit: int) -> list[tuple[int, float]]:
        """⚠️ ONE HIT PER DOCUMENT. Without this the top-10 is ten chunks of the same file —
        technically correct and useless."""
        if not self.refresh() or self._vectors is None or self._vectors.size == 0:
            return []
        sims = self._vectors @ np.asarray(qvec, dtype=np.float32).reshape(-1)
        order = np.argsort(-sims)[: max(limit * 4, 40)]
        out, seen = [], set()
        for i in order:
            did = int(self._ids[i])
            if did in seen:
                continue
            seen.add(did)
            out.append((did, float(sims[i])))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ staleness
    def check_stale(self, doc_count: int, doc_max_id: int, doc_mtime_sum: float) -> dict:
        """Has the index changed since these vectors were built?

        ⚠️⚠️ THIS IS THE LAST OF THE FOUR REQUIREMENTS AND THE ONE THAT PREVENTS SILENT WRONG
        ANSWERS. `--no-semantic` does not clear vectors, and an interrupted embedding run leaves
        the previous file in place. In both cases the app would serve semantic results computed
        against a DIFFERENT document set, with no indication.

        ⚠️ THE THREE SIGNALS ARE CHOSEN TO CATCH DIFFERENT THINGS:
            count     -> documents added or removed
            max_id    -> documents added (an id higher than any recorded)
            mtime_sum -> documents EDITED in place, which changes neither of the other two
        ⚠️ And mtime_sum is a float sum, so it is a heuristic, not a proof. It is reported as a
        warning rather than a guarantee, because claiming certainty here would be a lie.
        """
        if not self.man.shards:
            return {"stale": False, "reason": "no vectors built yet", "severity": "none"}
        if not self.man.complete:
            return {"stale": True, "severity": "incomplete",
                    "reason": f"embedding stopped part-way "
                              f"({len(self.man.done_doc_ids):,} of {doc_count:,} documents)",
                    "covered": len(self.man.done_doc_ids), "total": doc_count}
        drift = []
        if doc_count != self.man.doc_count:
            drift.append(f"document count changed ({self.man.doc_count:,} -> {doc_count:,})")
        if doc_max_id > self.man.doc_max_id:
            drift.append(f"{doc_max_id - self.man.doc_max_id:,} new document id(s)")
        if abs(doc_mtime_sum - self.man.doc_mtime_sum) > 1.0:
            drift.append("files were modified")
        if drift:
            return {"stale": True, "severity": "outdated", "reason": "; ".join(drift),
                    "built_at": self.man.updated}
        return {"stale": False, "reason": "vectors match the index", "severity": "none"}
