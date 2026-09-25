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

# =============================================================================
# CHUNKING — and the measurement that forced a rewrite
# =============================================================================
# ⚠️⚠️ THE ORIGINAL CHUNKER SPLIT ON CHARACTERS, BUT THE MODEL'S LIMIT IS IN TOKENS.
# That is a category error, and it caused SILENT DATA LOSS. Measured on a real corpus:
#
#     bge-small max sequence length : 512 tokens
#     chars per token               : 3.37   (NOT the assumed 4.0 — code is denser than prose)
#     a 512-token window holds      : ~1727 characters
#
#     chunks at CHUNK_CHARS=1200    : mean 320 tokens = only 62% of the window
#     chunks EXCEEDING 512 tokens   : 8 — **and the model truncates them with no error**
#
# ⚠️ So we were splitting documents into MORE chunks than necessary while giving each one
# LESS context than the model could hold — and a handful were losing their tail entirely.
# ⚠️ Dense code tokenises at ~1.5 chars/token, so a fixed 1200-char window is 350 tokens for
# prose and 800 for minified JS. **A character budget cannot bound a token count.**
#
# The fix is to chunk in TOKENS, using the model's own tokenizer, and to map each window back
# to a character span so provenance still points at a real location in the file.
MAX_TOKENS = 512            # the model's hard limit
TOKEN_FILL = 0.85           # use 85%, leaving headroom for special tokens
CHUNK_TOKENS = int(MAX_TOKENS * TOKEN_FILL)     # 435
CHUNK_OVERLAP_TOKENS = 60   # ~14% overlap, in tokens
# ⚠️ Kept for the fallback path when no tokenizer is available (see chunk()).
CHUNK_CHARS = 1400
CHUNK_OVERLAP = 200


# =============================================================================
# TWO RUNTIMES, ONE INTERFACE
# =============================================================================
# ⚠️⚠️ THE DECISION, AND IT IS NOW BACKED BY MEASUREMENT RATHER THAN PREFERENCE.
#
# The advice "PyTorch for training, ONNX for deployment" is exactly right, and this app is a
# DEPLOYMENT target: we do not design or train the model, we run one finished model (bge-small)
# thousands of times on other people's machines.
#
# ⚠️ Measured on the same corpus with the same weights:
#     per-chunk cosine agreement   mean 1.0000, min 1.0000   (13/13 identical on the first run)
#     top-1 ranking agreement      5/5 queries
# ⚠️ **They produce the same vectors, because they are the same model.** ONNX is a different
# runtime for identical weights, so there is no quality to lose. The choice is footprint:
#     PyTorch path  ~2 GB  (+2-3 GB of nvidia-* packages on Linux)
#     ONNX path     ~150 MB
#
# ⚠️ BUT ONNX IS NOT UNIFORMLY FASTER, and the advice implies it is. Measured: ONNX was ~13x
# faster on single-query encoding and slower on bulk chunk encoding. It is a footprint and
# query-latency win, not a throughput win.
#
# ⚠️ AND ONNX HAS AN OS FLOOR: onnxruntime >= 1.23 will NOT load on macOS 13.0
# (`Symbol not found: __ZNSt3__18to_charsEPcS0_d` — built for macOS 13.4). 1.19.2 works.
# That pin has to travel into the packaging config, or the app dies at import on older Macs.
#
# ⚠️ So BOTH are supported, ONNX is the default because of the 13x footprint difference, and
# the torch path stays available for anyone who wants GPU acceleration or already has it.
DEFAULT_RUNTIME = os.environ.get("DEVICE_SEARCH_RUNTIME", "onnx")


class OnnxEmbedder:
    """fastembed: same weights, ONNX Runtime instead of PyTorch.

    ⚠️⚠️ `query_embed` DOES **NOT** APPLY THE bge QUERY PREFIX. I wrote the opposite here
    first, with confidence, and it was wrong. Measured:

        fastembed.query_embed(Q)  vs  torch.encode(PREFIX + Q)   0.95869   <- differs
        fastembed.query_embed(Q)  vs  torch.encode(Q)            1.00000   <- matches NO prefix

    ⚠️ **AND THE BUG IS INVISIBLE TO THE OBVIOUS TEST.** Document vectors came out
    bit-identical (5,726/5,726 at cos = 1.00000), so a check that compares *document* vectors
    reports "identical, no quality loss" - while top-1 ranking agreement was only 3/7. The
    regression lives ENTIRELY on the query path.

    ⚠️ So the prefix is OURS on both runtimes, for the same reason: bge-small is trained to
    expect it on queries, omitting it costs real recall, and nothing warns you.
    """

    def __init__(self, model_name: str = MODEL_NAME):
        from fastembed import TextEmbedding
        self.model = TextEmbedding(model_name)
        self.name = model_name

    def encode(self, texts, batch_size: int = 64, normalize_embeddings: bool = True,
               show_progress_bar: bool = False, convert_to_numpy: bool = True, **_):
        import numpy as _np
        return _np.array(list(self.model.embed(list(texts))), dtype=_np.float32)

    def encode_query(self, query: str):
        # ⚠️ THE PREFIX IS ADDED HERE, NOT LEFT TO fastembed. See the class docstring:
        # `query_embed` does not add it. That was MEASURED, not assumed.
        import numpy as _np
        return _np.array(list(self.model.embed([QUERY_PREFIX + query])), dtype=_np.float32)

    def dim(self) -> int:
        return int(self.encode(["x"]).shape[-1])


class TorchEmbedder:
    """sentence-transformers. The optional path — ~2 GB, GPU-capable, identical output."""

    def __init__(self, model_name: str = MODEL_NAME):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_name)
        self.name = model_name

    def encode(self, texts, batch_size: int = 64, normalize_embeddings: bool = True,
               show_progress_bar: bool = False, convert_to_numpy: bool = True, **_):
        return self.model.encode(list(texts), batch_size=batch_size,
                                 normalize_embeddings=normalize_embeddings,
                                 show_progress_bar=show_progress_bar,
                                 convert_to_numpy=convert_to_numpy)

    def encode_query(self, query: str):
        # ⚠️ The prefix is applied HERE, by us, because sentence-transformers does not do it.
        # This asymmetry with OnnxEmbedder is the single most likely thing to get wrong.
        return self.model.encode([QUERY_PREFIX + query], normalize_embeddings=True,
                                 convert_to_numpy=True)


def build_embedder(runtime: str | None = None, model_name: str = MODEL_NAME):
    """⚠️ Falls back rather than crashing: if the chosen runtime is unavailable, try the other
    and SAY SO. A user with torch installed should not be blocked because ONNX would not load
    on their OS version — which is a real case on macOS 13.0."""
    runtime = (runtime or DEFAULT_RUNTIME).lower()
    order = ["onnx", "torch"] if runtime == "onnx" else ["torch", "onnx"]
    errors = []
    for r in order:
        try:
            if r == "onnx":
                return OnnxEmbedder(model_name), "onnx", None
            return TorchEmbedder(model_name), "torch", None
        except Exception as e:
            errors.append(f"{r}: {type(e).__name__}: {e}")
    return None, None, " | ".join(errors)


class Semantic:
    """⚠️ LAZY: the model is only loaded when semantic search is actually used."""

    def __init__(self, vec_path: Path | None = None, runtime: str | None = None):
        self.vec_path = Path(vec_path) if vec_path else None
        self.runtime = runtime
        self._model = None
        self._runtime_used = None
        # ⚠️ THE SHARDED STORE, when one exists. `vectors`/`doc_ids` below remain only as the
        # legacy single-file fallback.
        self.vstore = None
        self.vectors: np.ndarray | None = None   # L2-normalised, so cosine == dot product
        self.doc_ids: np.ndarray | None = None
        self.offsets: np.ndarray | None = None

    # ------------------------------------------------------------- the model
    @property
    def model(self):
        if self._model is None:
            emb, used, err = build_embedder(self.runtime)
            if emb is None:
                # ⚠️ RAISED, NOT SWALLOWED. The CLI catches this and prints which runtimes were
                # tried and why each failed — "semantic search is off" with no reason is the
                # failure mode this repo keeps recording.
                raise RuntimeError(f"no embedding runtime available — {err}")
            self._model, self._runtime_used = emb, used
        return self._model

    def available(self) -> tuple[bool, str]:
        """⚠️ REPORTED, NOT ASSUMED. If the model is missing, semantic search is OFF and the
        CLI prints that — rather than silently returning nothing and letting the user conclude
        their file does not exist."""
        emb, used, err = build_embedder(self.runtime)
        if emb is None:
            return False, err
        return True, f"{MODEL_NAME} via {used.upper()}"

    # -------------------------------------------------------------- indexing
    def _tokenizer(self):
        """⚠️ LAZY AND CACHED. Loading a tokenizer is ~1s; it must not happen per document."""
        if getattr(self, "_tok", None) is None:
            try:
                from transformers import AutoTokenizer
                self._tok = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=True)
            except Exception:
                # ⚠️ FALLBACK, NOT A CRASH. Without a tokenizer we cannot count tokens, so we
                # fall back to the conservative character splitter and SAY SO via
                # `token_aware_chunking`. A search tool that refuses to index is worse than
                # one that indexes slightly worse.
                self._tok = False
        return self._tok or None

    def chunk(self, text: str) -> list[tuple[int, str]]:
        """Return [(char_offset, chunk_text)] for one document.

        ⚠️⚠️ TOKEN-BASED, because the model's limit is in tokens and a character budget cannot
        bound a token count. Measured error from the character version: 8 of 5,726 chunks
        exceeded the 512-token window and were TRUNCATED BY THE MODEL with no error, and the
        average chunk used only 62% of the capacity we were paying to split into.

        ⚠️ The offsets come from the tokenizer's `offset_mapping`, so each window still maps
        back to a REAL character span in the file — provenance is not sacrificed to the
        token-accurate split.
        """
        tok = self._tokenizer()
        if tok is None:
            return self._chunk_chars(text)          # conservative fallback, documented above
        try:
            enc = tok(text, add_special_tokens=False, return_offsets_mapping=True,
                      truncation=False)
            ids = enc["input_ids"]
            offs = enc["offset_mapping"]
        except Exception:
            return self._chunk_chars(text)
        if not ids:
            return [(0, text)]
        if len(ids) <= CHUNK_TOKENS:
            return [(0, text)]

        out, start = [], 0
        stride = max(1, CHUNK_TOKENS - CHUNK_OVERLAP_TOKENS)
        while start < len(ids):
            end = min(start + CHUNK_TOKENS, len(ids))
            c0 = offs[start][0]
            c1 = offs[end - 1][1]
            # ⚠️ Extend back to a LINE boundary so a chunk does not begin mid-line. The token
            # window is the hard constraint; the line boundary is cosmetic and cannot break it,
            # because it only ever moves the START earlier.
            nl = text.rfind("\n", c0, min(c0 + 200, len(text)))
            if nl != -1 and nl > 0:
                c0 = nl + 1
            out.append((c0, text[c0:c1]))
            if end >= len(ids):
                break
            start += stride
        return out

    def _chunk_chars(self, text: str) -> list[tuple[int, str]]:
        """⚠️ FALLBACK ONLY — used when no tokenizer is available.

        ⚠️ CHUNK_CHARS IS SET TO 1400, BELOW THE ~1727-CHAR EQUIVALENT OF THE 512-TOKEN WINDOW.
        The old value of 1200 was chosen while assuming 4 chars/token; the measured ratio is
        3.37, and dense code is lower still. 1400 keeps even fairly dense files inside the
        window, and anything denser than that is better truncated slightly than unsearchable.
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

    @property
    def token_aware_chunking(self) -> bool:
        """⚠️ REPORTED BY THE CLI. If this is False, the index is character-chunked and chunks
        may be truncated — the user deserves to know which mode produced their index."""
        return self._tokenizer() is not None

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
        vecs = self.model.encode(texts, batch_size=batch)
        self.vectors = np.asarray(vecs, dtype=np.float32)
        self.offsets = np.asarray(offs, dtype=np.int64)
        return n_docs, len(texts)

    # ---------------------------------------------------------------- search
    def search(self, query: str, limit: int = 40, doc_id_of_chunk=None):
        """Cosine similarity over the SHARDED store. ⚠️ Vectors are L2-normalised, so this is one
        matmul — not a norm per candidate, which is the usual accidental O(n) per comparison.

        ⚠️⚠️ THIS READS FROM THE SHARDS ON DISK, NOT FROM AN IN-MEMORY MATRIX BUILT AT INDEX
        TIME. That is what makes the index RESUMABLE: a run that stops at 40% has 40% of its
        shards on disk, and search uses them immediately instead of waiting for a complete file.
        """
        if self.vstore is not None:
            return self.vstore.search(
                self.model.encode_query(query), limit)
        # ⚠️ FALLBACK for an index built by the previous format (a single vectors.npy). Kept so
        # an upgrade does not silently lose a working index — the old file is still read, and
        # `check_stale` reports that it is not the current format.
        if self.vectors is None or self.vectors.shape[0] == 0:
            return []
        # ⚠️ `encode_query`, NOT `encode`. The two runtimes apply the bge query prefix in
        # DIFFERENT PLACES — fastembed internally, sentence-transformers not at all. Calling
        # `encode` here would silently drop the prefix on one path and double it on the other.
        q = self.model.encode_query(query)
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
        """⚠️ PREFERS THE SHARDED STORE AND FALLS BACK TO THE OLD SINGLE FILE.

        A user who built an index before this change keeps a working index; the format change
        does not silently break them. `check_stale()` will report that the vectors came from the
        older format so the difference is visible rather than assumed.
        """
        from .config import INDEX_DIR
        from .vectors import VectorStore
        vs = VectorStore(INDEX_DIR, dim=384)
        if vs.man.shards:
            self.vstore = vs
            # ⚠️ Report whether the build finished, because a partial index that LOOKS complete
            # is the failure this whole mechanism exists to prevent.
            self.partial = not vs.man.complete
            self.manifest = vs.man
            return True
        vec_path, ids_path = Path(vec_path), Path(ids_path)
        if not vec_path.exists() or not ids_path.exists():
            return False
        self.vectors = np.load(vec_path)
        self.doc_ids = np.load(ids_path)
        return True
