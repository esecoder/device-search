"""Image understanding: match a written description to a picture.

This is the layer that answers "a red car parked near a house". OCR cannot - it
reads text printed inside the image and a photograph has none. The text embedding
model cannot either: bge-small has never seen a pixel.

SigLIP2 maps an image and a sentence into the SAME 768-dimensional space, so a
description and a picture can be compared directly. Two halves, one space:

    ImageEmbedding("google/siglip2-base-patch16-224")   image -> vector
    TextEmbedding("google/siglip2-base-patch16-224")   query -> vector

They must be the same model name. Pairing the vision half with a different text
model produces two vectors that are individually sensible and mutually
meaningless, and the failure looks like poor search rather than a bug.

Measured on this machine: 0.22s per image, 768 dimensions, about 3 KB per vector.

    image embed    0.22 s each
    1,660 images   about 6 minutes, 5 MB of vectors

WHAT IT IS GOOD AND BAD AT

Good: "what is this a picture of", "a bar chart", "a screenshot of code".

Weak: counting ("three cars"), spatial relations ("left of"), and small details.
SigLIP sees the whole image at 224x224; it does not read the layout. For those
questions a captioning model that writes a sentence is the right tool, and that
is a separate, more expensive step.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

# Same name for both halves. See the note above: a mismatched pair is silent.
MODEL_NAME = "google/siglip2-base-patch16-224"
DIM = 768

# Images smaller than this are almost certainly icons, spacers or thumbnails, and
# embedding them costs as much as a real picture for no value.
MIN_BYTES = 3_000
MIN_SIDE = 64


def _looks_worth_embedding(path: Path) -> bool:
    try:
        if path.stat().st_size < MIN_BYTES:
            return False
    except OSError:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return w >= MIN_SIDE and h >= MIN_SIDE
    except Exception:
        return False


class ImageIndex:
    """Embedded images, by document id.

    Stored as two parallel arrays rather than one object per image, because the
    search is a single matrix multiply and that is what numpy is good at.
    """

    def __init__(self, model_name: str = MODEL_NAME):
        self.model_name = model_name
        self._img = None
        self._txt = None
        self.doc_ids: list[int] = []
        self.vectors: np.ndarray | None = None

    # ------------------------------------------------------------------ models
    def _image_model(self):
        if self._img is None:
            from fastembed import ImageEmbedding
            t = time.time()
            self._img = ImageEmbedding(model_name=self.model_name)
            print(f"  image model ready in {time.time()-t:.0f}s")
        return self._img

    def _text_model(self):
        if self._txt is None:
            from fastembed import TextEmbedding
            t = time.time()
            self._txt = TextEmbedding(model_name=self.model_name)
            print(f"  text model ready in {time.time()-t:.0f}s")
        return self._txt

    # ------------------------------------------------------------------ build
    def embed_images(self, items, batch: int = 32, progress_every: int = 50):
        """items: iterable of (doc_id, path). Returns the number embedded.

        Images that cannot be opened are skipped rather than failing the build:
        a corrupt JPEG in the corpus should cost that one image, not the index.
        """
        model = self._image_model()
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        skipped = 0
        t0 = time.time()

        batch_ids, batch_paths = [], []

        def flush():
            nonlocal skipped
            if not batch_paths:
                return
            alive_ids, alive_paths = [], []
            for i, p in zip(batch_ids, batch_paths):
                if _looks_worth_embedding(Path(p)):
                    alive_ids.append(i)
                    alive_paths.append(p)
            if alive_paths:
                try:
                    out = list(model.embed(alive_paths))
                    for i, v in zip(alive_ids, out):
                        ids.append(i)
                        vecs.append(np.asarray(v, dtype=np.float32))
                except Exception:
                    # One bad file can abort a whole batch. Fall back to one at a
                    # time so the rest survive.
                    for i, p in zip(alive_ids, alive_paths):
                        try:
                            v = list(model.embed([p]))[0]
                            ids.append(i)
                            vecs.append(np.asarray(v, dtype=np.float32))
                        except Exception:
                            skipped += 1
            skipped += len(batch_ids) - len(alive_ids)
            batch_ids.clear()
            batch_paths.clear()

        for doc_id, path in items:
            batch_ids.append(doc_id)
            batch_paths.append(str(path))
            if len(batch_ids) >= batch:
                flush()
                done = len(ids)
                if done and done % progress_every < batch:
                    rate = done / max(time.time() - t0, 1e-6)
                    print(f"  … {done:,} images embedded ({rate:.1f}/s)", flush=True)

        flush()

        self.doc_ids = ids
        if vecs:
            m = np.vstack(vecs)
            # Normalised once at build time, so search is a dot product and not a
            # division per query per row.
            self.vectors = m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-9, None)
        else:
            self.vectors = np.zeros((0, DIM), dtype=np.float32)
        return len(ids), skipped

    # ------------------------------------------------------------------ search
    def search(self, query: str, k: int = 20) -> list[tuple[int, float]]:
        """Return (doc_id, similarity) for the images closest to the description."""
        if self.vectors is None or len(self.doc_ids) == 0:
            return []
        q = np.asarray(list(self._text_model().embed([query]))[0], dtype=np.float32)
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        sims = self.vectors @ q
        k = min(k, len(sims))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(self.doc_ids[i], float(sims[i])) for i in top]

    # ------------------------------------------------------------------ persist
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / "image_vectors.npy", self.vectors)
        np.save(directory / "image_ids.npy", np.asarray(self.doc_ids, dtype=np.int64))
        (directory / "image_model.txt").write_text(self.model_name)

    def load(self, directory: Path) -> bool:
        v = directory / "image_vectors.npy"
        i = directory / "image_ids.npy"
        if not (v.exists() and i.exists()):
            return False
        try:
            self.vectors = np.load(v)
            self.doc_ids = [int(x) for x in np.load(i)]
            return len(self.doc_ids) > 0
        except Exception:
            return False


def demo(image_paths: list[str], queries: list[str]) -> None:
    """Show the matrix, so the result can be read rather than trusted."""
    idx = ImageIndex()
    model = idx._image_model()
    t = time.time()
    imgs = np.array(list(model.embed(image_paths)))
    dt = time.time() - t
    txts = np.array(list(idx._text_model().embed(queries)))
    imgs = imgs / np.linalg.norm(imgs, axis=1, keepdims=True)
    txts = txts / np.linalg.norm(txts, axis=1, keepdims=True)
    sims = imgs @ txts.T

    print(f"  {len(image_paths)} images embedded in {dt:.1f}s ({dt/len(image_paths):.2f}s each)")
    print()
    header = "".join(f"{q[:12]:>14}" for q in queries)
    print(f"  {'':<20}{header}")
    hits = 0
    for r, p in enumerate(image_paths):
        row = "".join(f"{sims[r][c]:>14.3f}" for c in range(len(queries)))
        best = int(sims[r].argmax())
        ok = best == r if r < len(queries) else None
        hits += bool(ok)
        mark = "OK" if ok else ("WRONG" if ok is False else "")
        print(f"  {os.path.basename(p)[:19]:<20}{row}   {queries[best][:24]!r} {mark}")
    print()
    print(f"  matched to their own description: {hits}/{len(image_paths)}"
          f"   (chance {1/len(queries):.0%})")
