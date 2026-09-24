"""
semantic.py — local embeddings, so "where do I configure X" works.

⚠️ WHY THIS IS LOCAL AND NOT AN API. A device search tool that embeds your files sends every
file it indexes to a third party. That is not a performance trade-off, it is a disclosure, and
it happens at INDEX time — before you ever type a query. The local model is ~130 MB and runs on
CPU at a few hundred documents a second, which is fast enough that the API version buys nothing.

⚠️ AND WHY IT IS THE THIRD BACKEND, NOT THE FIRST. Embeddings are genuinely bad at the user's
own example: `InputLayer(shape=(784,))` is a literal string, and a semantic model returns
"things about Keras input layers" while the actual line sits unretrieved. **Embeddings answer
questions about MEANING; they are the wrong tool for questions about TEXT.** A tool that only
embeds would fail the query that motivated this project.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

# ⚠️ SAME MODEL AS THE RAG TRACK. Using a different one here would make the two projects
# disagree about the same query and the disagreement would be uninterpretable.
MODEL_NAME = os.environ.get("DEVICE_SEARCH_MODEL", "BAAI/bge-small-en-v1.5")
# ⚠️ bge models are trained with this prefix on the QUERY side only. Omitting it costs several
# points of recall and is invisible — the search still "works", just worse.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# ⚠️ CHUNKING: 1200 characters with overlap. A whole 2 MB file in one vector averages every
# topic in it into a blur, and a 200-character chunk loses the context that disambiguates it.
# ⚠️ But chunks need to map back to a FILE for display, so each vector stores its doc_id and
# the character offset it came from — otherwise a hit points at an unopenable fragment.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200


class Semantic:
    """⚠️ LAZY: the model is only loaded when semantic search is actually used."""

    def __init__(self, vec_path: Path | None = None):
        self.vec_path = Path(vec_path) if vec_path else None
        self._model = None
        self.vectors: np.ndarray | None = None   # L2-normalised, so cosine == dot product
        self.doc_ids: np.ndarray | None = None
        self.offsets: np.ndarray | None = None

    # ------------------------------------------------------------- the model
    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(MODEL_NAME)
        return self._model

    def available(self) -> tuple[bool, str]:
        """⚠️ REPORTED, NOT ASSUMED. If the model is missing, semantic search is OFF and the
        CLI prints that — rather than silently returning nothing and letting the user conclude
        their file does not exist."""
        try:
            import sentence_transformers  # noqa: F401
            return True, MODEL_NAME
        except Exception as e:
            return False, f"sentence-transformers unavailable ({type(e).__name__})"

    # -------------------------------------------------------------- indexing
    def chunk(self, text: str) -> list[tuple[int, str]]:
        """Return [(char_offset, chunk_text)] for one document.

        ⚠️ SPLITS ON LINE BOUNDARIES, NOT CHARACTERS. A fixed character cut lands mid-identifier
        and produces a chunk beginning "…(shape=(784,))" with no idea what it belongs to.
        """
        if len(text) <= CHUNK_CHARS:
            return [(0, text)]
        out, start = [], 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))
            if end < len(text):
                nl = text.rfind("\n", start, end)
                if nl > start + CHUNK_CHARS // 2:
                    end = nl
            out.append((start, text[start:end]))
            if end >= len(text):
                break
            start = max(end - CHUNK_OVERLAP, start + 1)
        return out

    def build(self, docs, batch: int = 64, progress_every: int = 5000):
        """Embed every chunk of every document. `docs` yields FileDoc.

        ⚠️ BATCHED, because encoder throughput is dominated by per-call overhead at batch=1 —
        the same lesson as the dataloader benchmark in the GPU track. One document at a time on
        this model is roughly 10x slower for identical output.
        """
        texts, ids, offs = [], [], []
        n_docs = 0
        for d in docs:
            n_docs += 1
            for off, chunk in self.chunk(d.text):
                texts.append(chunk)
                ids.append(0)          # filled in by the caller via `doc_ids`
                offs.append(off)
            if progress_every and n_docs % progress_every == 0:
                print(f"    … {n_docs:,} docs, {len(texts):,} chunks", flush=True)
        if not texts:
            self.vectors = np.zeros((0, 384), dtype=np.float32)
            self.doc_ids = np.zeros(0, dtype=np.int64)
            self.offsets = np.zeros(0, dtype=np.int64)
            return n_docs, 0
        vecs = self.model.encode(texts, batch_size=batch, normalize_embeddings=True,
                                 show_progress_bar=False, convert_to_numpy=True)
        self.vectors = np.asarray(vecs, dtype=np.float32)
        self.offsets = np.asarray(offs, dtype=np.int64)
        return n_docs, len(texts)

    # ---------------------------------------------------------------- search
    def search(self, query: str, limit: int = 40, doc_id_of_chunk=None):
        """Cosine similarity. ⚠️ Vectors are already L2-normalised, so this is one matmul —
        not a norm per candidate, which is the usual accidental O(n) per comparison."""
        if self.vectors is None or self.vectors.shape[0] == 0:
            return []
        q = self.model.encode([QUERY_PREFIX + query], normalize_embeddings=True,
                              convert_to_numpy=True)
        sims = self.vectors @ np.asarray(q, dtype=np.float32).reshape(-1)
        top = np.argsort(-sims)[:limit * 3]
        out = []
        seen = set()
        for i in top:
            key = int(self.doc_ids[i]) if self.doc_ids is not None else int(i)
            # ⚠️ ONE HIT PER DOCUMENT. Without this the top-10 is ten chunks of the same file,
            # which is technically correct and useless — the same dedup lesson as the
            # document-level nDCG bug in the RAG track.
            if key in seen:
                continue
            seen.add(key)
            out.append((key, float(sims[i])))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ persistence
    def save(self, vec_path: Path, doc_ids, ids_path: Path) -> None:
        vec_path = Path(vec_path)
        vec_path.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ chmod 600: these vectors are a lossy encoding of file contents and are exactly as
        # sensitive as the index itself.
        np.save(vec_path, self.vectors if self.vectors is not None else np.zeros((0, 384),
                                                                               dtype=np.float32))
        np.save(ids_path, np.asarray(doc_ids, dtype=np.int64))
        self.doc_ids = np.asarray(doc_ids, dtype=np.int64)
        try:
            os.chmod(vec_path, 0o600)
            os.chmod(ids_path, 0o600)
        except OSError:
            pass

    def load(self, vec_path: Path, ids_path: Path) -> bool:
        vec_path, ids_path = Path(vec_path), Path(ids_path)
        if not vec_path.exists() or not ids_path.exists():
            return False
        self.vectors = np.load(vec_path)
        self.doc_ids = np.load(ids_path)
        return True
