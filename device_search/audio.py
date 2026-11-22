"""Audio: transcribe it, then let it be searched like anything else.

A recording is the one kind of file where the answer exists and nothing can find
it. There is no text to grep, no filename that says what was said, and the person
who needs the quote does not remember which of forty recordings it is in.

The approach is the same one that made PDFs work: turn the audio into text with a
marker for WHERE in the audio each piece came from, then let the ordinary text
backends index it. Nothing downstream needs to know it was ever audio.

    [0:00] We need to decide on the database before Friday.
    [0:14] Postgres, unless the volume is higher than we think.
    [0:22] Agreed. I will check with the infrastructure team.

WHY TIMESTAMPS AND NOT JUST A TRANSCRIPT

"somewhere in this 52-minute meeting" is not an answer. With a marker per segment,
a hit can point at 14:22 and the user can play from there. This is the same reason
pdftext pages are kept: a citation into a document with no position is not a
citation.

WHY SEGMENT MARKERS RATHER THAN WORD TIMESTAMPS

Word-level timing triples the stored size and is useful for subtitles, not for
search. A segment is what Whisper produces natively and what a person thinks in.

COST

Whisper is the slowest extractor in this project by a wide margin. Measured on
this machine with the "tiny" model: roughly 1 second of audio processing per
1 second of audio on CPU with the base model, faster with tiny. A one-hour
recording is therefore minutes, not seconds, and it must be opt-in with the cost
shown - exactly like the code index and the image index.

The model is downloaded on first use (~75 MB for tiny) into the HuggingFace cache.
"""

from __future__ import annotations

import re
from pathlib import Path

# Which Whisper checkpoint. "tiny" is 75 MB and enough to make speech findable;
# "base" is 145 MB and noticeably better on accented speech. Left at tiny because
# the point is retrieval, and a search hit that is 90% right still takes the user
# to the right minute of the right recording.
DEFAULT_MODEL = "tiny"

# Audio and video both go through this: a video's audio track is usually the part
# that answers a question, and `av` reads the container either way.
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

# A segment shorter than this is a breath or a single word and adds noise.
MIN_SEGMENT_CHARS = 2


def stamp(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS.

    Not a clock time: these are offsets into a recording, and 0:14 means fourteen
    seconds in, not quarter past midnight.
    """
    s = max(int(seconds), 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def parse_stamp(text: str) -> float | None:
    """Read the first [M:SS] or [H:MM:SS] marker in a snippet, in seconds.

    Mirrors answer.page_span(): the search layer uses this to tell the interface
    where in the audio a hit came from, without the interface knowing the format.
    """
    m = re.search(r"\[(\d+):(\d{2}):(\d{2})\]", text or "")
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.search(r"\[(\d+):(\d{2})\]", text or "")
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def time_span(snippet: str) -> tuple[float, float] | None:
    """The range a snippet covers, from its markers. None when it has none."""
    marks = re.findall(r"\[(\d+):(\d{2})(?::(\d{2}))?\]", snippet or "")
    if not marks:
        return None
    values = []
    for a, b, c in marks:
        if c:
            values.append(int(a) * 3600 + int(b) * 60 + int(c))
        else:
            values.append(int(a) * 60 + int(b))
    return min(values), max(values)


def decode_mono_16k(path: Path):
    """Decode any container av can read into float32 mono at 16 kHz.

    faster-whisper calls `av.open(..., metadata_errors="ignore")`, and av 19
    removed that keyword. Measured: TypeError on every file, which the caller saw
    as "audio_unavailable" with no reason - the bare except in transcribe() was
    hiding a version mismatch.

    Decoding here and passing an array bypasses faster-whisper's decoder entirely,
    so the incompatibility cannot return. `av` is already a dependency of
    faster-whisper, so this adds nothing to the install.
    """
    import av
    import numpy as np
    chunks = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return None
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def has_speech_level(audio, threshold: float = 0.01) -> bool:
    """Whether a waveform has enough amplitude to contain speech.

    A silent file transcribes to nothing, and reporting that as a failure would
    blame Whisper for an empty recording. Measured on a real file: peak amplitude
    0.017, which is room tone.
    """
    if audio is None or len(audio) == 0:
        return False
    import numpy as np
    return float(np.abs(audio).max()) >= threshold


class Transcriber:
    """Whisper, loaded lazily so importing this costs nothing."""

    def __init__(self, model_size: str = DEFAULT_MODEL):
        self.model_size = model_size
        self._model = None
        self.load_seconds = 0.0
        self.last_error = ""

    def _load(self):
        if self._model is None:
            import time
            from faster_whisper import WhisperModel
            t0 = time.time()
            # CPU with int8: the fastest CPU configuration, and this machine has no
            # usable GPU path in CUDA terms. int8 costs a little accuracy and a lot
            # of time, which is the right trade for making speech findable.
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            self.load_seconds = time.time() - t0
        return self._model

    def transcribe(self, path: Path, max_seconds: int | None = None) -> tuple[str, str]:
        """Return (marked_text, method). Never raises: a bad file is a method name.

        The caller stores the text exactly as returned. A failure returns
        ("", "audio_unavailable") or ("", "audio_empty") so a count of failures is
        visible in `ds stats` - an empty document is a normal thing to store, which
        is how 255 unreadable PDFs went unnoticed for so long.
        """
        try:
            model = self._load()
        except Exception:
            return "", "audio_unavailable"

        # Decoded here, not by faster-whisper: see decode_mono_16k.
        try:
            audio = decode_mono_16k(path)
        except Exception as exc:
            self.last_error = f"decode: {type(exc).__name__}: {exc}"
            return "", "audio_unavailable"
        if audio is None or len(audio) == 0:
            return "", "audio_empty"
        if not has_speech_level(audio):
            # Room tone, not speech. Saying "unavailable" would blame the model.
            return "", "audio_silent"

        try:
            segments, info = model.transcribe(
                audio,
                beam_size=1,        # greedy: faster, and the difference is small
                vad_filter=True,    # skip silence, which is most of a long recording
                language=None,      # detect; a device can hold more than one language
                condition_on_previous_text=False,   # avoids repetition loops on music
            )
        except Exception as exc:
            # The reason is KEPT. A bare except that returns a status name made a
            # version mismatch look like an unsupported file for an hour.
            self.last_error = f"{type(exc).__name__}: {exc}"
            return "", "audio_unavailable"

        out: list[str] = []
        try:
            for seg in segments:
                if max_seconds and seg.start > max_seconds:
                    break
                text = (seg.text or "").strip()
                if len(text) < MIN_SEGMENT_CHARS:
                    continue
                out.append(f"[{stamp(seg.start)}] {text}")
        except Exception:
            # A decoder failure mid-file should keep what was transcribed rather
            # than discarding the readable half.
            if not out:
                return "", "audio_unavailable"

        if not out:
            return "", "audio_empty"
        return "\n".join(out), "whisper"


def duration_seconds(path: Path) -> float | None:
    """Length of a media file, for showing the cost before transcribing it."""
    try:
        import av
        with av.open(str(path)) as container:
            if container.duration:
                return container.duration / 1_000_000
            return None
    except Exception:
        return None


def locate(text: str, query: str) -> float | None:
    """Seconds into the recording where `query` occurs.

    parse_stamp() returns the FIRST marker in the document, which is 0:00 for
    every hit. Measured: searching a transcript for 'infrastructure' reported
    "audio starts at 0s" when the word is spoken at 0:07 - correct for the file,
    useless for the user, who then scrubs through the whole recording.

    So this finds the match, then walks BACK to the nearest marker before it. A
    marker is written once per segment, so the nearest preceding one is the start
    of the segment containing the word.
    """
    if not text or not query:
        return None
    needle = query.strip().lower()
    if not needle:
        return None
    pos = (text or "").lower().find(needle)
    if pos < 0:
        return None
    before = text[:pos]
    marks = list(re.finditer(r"\[(\d+):(\d{2})(?::(\d{2}))?\]", before))
    if not marks:
        return None
    a, b, c = marks[-1].groups()
    if c:
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


def snippet_at(text: str, query: str, radius: int = 160) -> str:
    """The text around a match, for showing the line rather than the whole file."""
    if not text or not query:
        return ""
    pos = (text or "").lower().find(query.strip().lower())
    if pos < 0:
        return text[:radius]
    start = max(0, pos - radius // 2)
    return text[start:start + radius].strip()
