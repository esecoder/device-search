"""Audio: transcribe a real recording, then find a phrase inside it."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_search.audio import Transcriber, locate, time_span
from device_search.store import Store

SOURCE = Path("/tmp/speech-test.aiff")

# What the recording actually says. macOS `say` wrote it, so the ground truth is
# exact rather than a judgement about whether a transcript is good.
SAID = [
    "database",
    "Friday",
    "infrastructure",
]


class Doc:
    def __init__(self, path, text):
        self.path = path
        self.mtime = 1.0
        self.size = len(text)
        self.lang = "audio"
        self.n_lines = 1
        self.text = text
        self.method = "whisper"
        self.is_dir = False


def main() -> int:
    if not SOURCE.exists():
        print("  SKIP  no test recording (create one with: say -o /tmp/speech-test.aiff '...')")
        return 0

    transcriber = Transcriber()
    text, method = transcriber.transcribe(SOURCE)
    print(f"  method : {method}")
    print(f"  chars  : {len(text):,}")
    for line in text.split("\n")[:4]:
        print(f"    {line}")

    if method != "whisper" or not text:
        print(f"  FAIL  transcription produced nothing ({transcriber.last_error})")
        return 1

    print()
    print(f"  timestamps present   : {time_span(text)}")
    if not time_span(text):
        print("  FAIL  no [M:SS] markers, so a hit cannot point into the audio")
        return 1

    store = Store(Path(tempfile.mkdtemp()) / "audio.db")
    store.add_many([Doc("/media/standup.m4a", text)])
    store.conn.commit()

    print()
    print("  searching inside the recording")
    found = 0
    for q in SAID:
        hits = store.keyword_search(q, limit=3)
        if not hits:
            print(f"    {q!r:<20} no hit")
            continue
        row = store.by_id(hits[0][0])
        # locate(), not parse_stamp(): the first marker in a long recording is
        # always 0:00, and a timestamp that says "0s" for a word spoken at 14:22
        # is worse than none because it looks precise.
        at = locate(row[4], q)
        if at is None:
            print(f"    {q!r:<20} found but no timestamp")
            continue
        found += 1
        print(f"    {q!r:<20} found at {at[0]}:{at[1]:02d}" if isinstance(at, tuple)
              else f"    {q!r:<20} found at {at:.0f}s")

    # At least one word must be located AFTER the first segment, or the timestamps
    # are not doing anything a 0:00 constant would not do.
    located_late = any(
        (locate(store.by_id(store.keyword_search(q, limit=1)[0][0])[4], q) or 0) > 0
        for q in SAID if store.keyword_search(q, limit=1))
    if not located_late:
        print("  FAIL  every timestamp is 0 - the markers are not being used")
        return 1

    print()
    if found == len(SAID):
        print(f"  PASS  {found}/{len(SAID)} spoken words are findable, with a timestamp")
        return 0
    print(f"  FAIL  only {found}/{len(SAID)} spoken words are findable")
    return 1


if __name__ == "__main__":
    sys.exit(main())
