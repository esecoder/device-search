"""Video: find the moment something is shown, not just the file it is in.

A video has two separable contents, and they need different tools:

    the audio track      -> Whisper  -> what was SAID, with a timestamp
    the frames           -> SigLIP   -> what was SHOWN, with a timestamp

Both are already built for other file types. This module adds the one thing video
needs that neither has: SAMPLING. A 40-minute recording is 60,000 frames, and the
whole problem is deciding which 60 of them are worth keeping.

    frame  ->  768-dim vector  ->  searched by describing it
    hit    ->  "at 12:30 the screen shows a bar chart"

WHY EVEN SAMPLING AND NOT EVERY FRAME

Adjacent frames at 30 fps are identical for search purposes. Sampling every N
seconds keeps frames that are far apart in time and therefore likely to differ.
Measured cost with SigLIP: 0.22 s per frame, so a 10-minute video at one frame
every 10 seconds is 60 frames and about 13 seconds of work.

WHY NOT SCENE DETECTION

Scene detection is better in principle and worse in practice here: it needs a
threshold, the threshold differs per video (a screencast never "cuts"), and a
wrong threshold silently returns zero frames. A fixed interval cannot fail - it
just costs more on static video, where the extra frames are cheap.

WHAT IT CANNOT DO

It cannot describe motion, and it cannot read small text on a slide at 224x224.
Those need a captioning model or a full-resolution OCR pass. This makes video
FINDABLE; it does not make it understood.
"""

from __future__ import annotations

import time
from pathlib import Path

# One frame every this many seconds. 10 s is the balance: a 10-minute video gives
# 60 frames (~13 s of embedding), and anything visible for less than 10 s is
# usually not what someone searches for later.
DEFAULT_INTERVAL = 10.0

# Below this, adjacent frames are near-duplicates and cost more than they add.
MIN_INTERVAL = 2.0

# Frames below this size are thumbnails or corrupt; SigLIP cannot do anything
# useful with them and they add noise to every query.
MIN_FRAME_SIDE = 64

# Frames are downscaled to this before embedding. SigLIP resizes to 224x224
# internally, so a 2652x1500 screen recording was paying to decode and resample
# twenty times more pixels than the model reads. Measured: 2.55 s per frame at
# full resolution, and the model's input is 224 samples square either way.
#
# 512 rather than 224 because the downscale filter matters: shrinking 2652 -> 224
# in one step aliases, and small text on a slide becomes noise.
FRAME_SIDE = 512


def stamp(seconds: float) -> str:
    s = max(int(seconds), 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def sample_frames(path: Path, interval: float = DEFAULT_INTERVAL):
    """Yield (timestamp_seconds, PIL.Image) at a fixed interval.

    SEEKS rather than decoding every frame. The first version decoded the whole
    stream and kept every Nth frame, which reads 5,670 frames to keep 19 - measured
    at 2.55 s per KEPT frame, and unchanged when the frames were downscaled or when
    the temp-file round trip was removed. The cost was the decode, not the model.

    Seeking costs a keyframe lookup per sample. A decoder must start at a keyframe,
    so each seek lands slightly BEFORE the requested time and the first frame at or
    after the target is kept.
    """
    import av

    interval = max(float(interval), MIN_INTERVAL)
    out = []

    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                return out
            stream = container.streams.video[0]
            duration = (container.duration / 1_000_000) if container.duration else None
            if not duration:
                return out

            target = 0.0
            while target < duration and len(out) < 600:
                # ⚠️ THE OFFSET IS IN THE STREAM'S TIME BASE, NOT MICROSECONDS.
                #
                # This stream's time_base is 1/6000. Passing target * 1,000,000
                # therefore asked for a point 167x further into the file than
                # intended, so every seek landed at the end and 18 of 19 samples
                # came back at 188.9s on a 189-second video. The samples were
                # wrong, not just slow - and they still looked like a plausible
                # list of timestamps.
                try:
                    tb = stream.time_base
                    offset = int(target / float(tb)) if tb else int(target * 1000)
                    container.seek(offset, stream=stream, backward=True)
                except Exception:
                    break

                picked = None
                for frame in container.decode(stream):
                    if frame.time is None:
                        continue
                    if frame.time + 1e-6 < target:
                        continue          # still before the target; keep reading
                    try:
                        img = frame.to_image()
                    except Exception:
                        break
                    if img.width < MIN_FRAME_SIDE or img.height < MIN_FRAME_SIDE:
                        break
                    if max(img.width, img.height) > FRAME_SIDE:
                        img.thumbnail((FRAME_SIDE, FRAME_SIDE))
                    picked = (float(frame.time), img)
                    break

                if picked:
                    out.append(picked)
                target += interval
    except Exception:
        # Whatever was sampled before the failure is still usable.
        pass
    return out


def duration_seconds(path: Path) -> float | None:
    try:
        import av
        with av.open(str(path)) as container:
            return (container.duration / 1_000_000) if container.duration else None
    except Exception:
        return None


def estimate_frames(path: Path, interval: float = DEFAULT_INTERVAL) -> int:
    d = duration_seconds(path)
    if d is None:
        return 0
    return max(int(d / max(interval, MIN_INTERVAL)), 1)


class VideoIndex:
    """Frame vectors, keyed by (document id, timestamp).

    Deliberately a flat list rather than one vector per video: a search for "a
    chart" should return the MOMENT, and collapsing a video to one average vector
    would make every hit point at the start.
    """

    DIM = 768

    def __init__(self, model_name: str | None = None):
        from .image_search import MODEL_NAME
        self.model_name = model_name or MODEL_NAME
        self._model = None
        self.doc_ids: list[int] = []
        self.times: list[float] = []
        self.paths: list[str] = []
        self.vectors = None

    def _embedder(self):
        if self._model is None:
            from .image_search import ImageIndex
            self._model = ImageIndex(self.model_name)
        return self._model

    def add_video(self, doc_id: int, path: Path, interval: float = DEFAULT_INTERVAL,
                  progress_every: int = 40) -> int:
        """Sample and embed one video. Returns the number of frames added."""
        frames = sample_frames(path, interval)
        if not frames:
            return 0
        model = self._embedder()._image_model()
        import numpy as np

        added = 0
        t0 = time.time()

        # PIL images go straight into embed(). The first version wrote every frame
        # to a temporary PNG and passed the path, because that is what the
        # still-image code does - but embed() accepts PIL.Image directly, and the
        # round trip dominated everything: measured 2.58s per frame with temp files
        # against 0.22s for a still image of the same model.
        BATCH = 16
        for i in range(0, len(frames), BATCH):
            batch = frames[i:i + BATCH]
            try:
                vecs = list(model.embed([img for _ts, img in batch]))
            except Exception:
                continue
            for (ts, _img), v in zip(batch, vecs):
                arr = np.asarray(v, dtype=np.float32)
                arr = arr / max(float(np.linalg.norm(arr)), 1e-9)
                self.doc_ids.append(doc_id)
                self.times.append(ts)
                self.paths.append(str(path))
                self.vectors = arr[None, :] if self.vectors is None \
                    else np.vstack([self.vectors, arr])
                added += 1
            if progress_every and added and added % progress_every < BATCH:
                rate = added / max(time.time() - t0, 1e-6)
                print(f"  … {added} frames ({rate:.1f}/s)", flush=True)
        return added

    def search(self, query: str, k: int = 10):
        """Return [(doc_id, timestamp_seconds, score)] for frames matching a description."""
        if self.vectors is None or not len(self.doc_ids):
            return []
        import numpy as np
        model = self._embedder()
        q = np.asarray(list(model._text_model().embed([query]))[0], dtype=np.float32)
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        sims = self.vectors @ q
        k = min(k, len(sims))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(self.doc_ids[i], self.times[i], float(sims[i])) for i in top]

    def save(self, directory: Path) -> None:
        import numpy as np
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / "video_vectors.npy", self.vectors)
        np.save(directory / "video_ids.npy", np.asarray(self.doc_ids, dtype=np.int64))
        np.save(directory / "video_times.npy", np.asarray(self.times, dtype=np.float32))
        (directory / "video_paths.txt").write_text("\n".join(self.paths))
        (directory / "video_model.txt").write_text(self.model_name)

    def load(self, directory: Path) -> bool:
        import numpy as np
        v = directory / "video_vectors.npy"
        if not v.exists():
            return False
        try:
            self.vectors = np.load(v)
            self.doc_ids = [int(x) for x in np.load(directory / "video_ids.npy")]
            self.times = [float(x) for x in np.load(directory / "video_times.npy")]
            p = directory / "video_paths.txt"
            self.paths = p.read_text().split("\n") if p.exists() else []
            return len(self.doc_ids) > 0
        except Exception:
            return False
