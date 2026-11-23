"""Video: the frames are sampled at even intervals, and a hit has a timestamp."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_search.video import (DEFAULT_INTERVAL, VideoIndex, duration_seconds,
                                 sample_frames, stamp)

# A recording that exists on this machine. Any real video would do; the test is
# about SAMPLING, not about this particular content.
CANDIDATES = [
    Path.home() / "Desktop/Post-Bridge Screenshots/Screen Recording 2024-11-30 at 1.25.33 AM.mov",
]


def main() -> int:
    video = next((p for p in CANDIDATES if p.exists()), None)
    if video is None:
        print(" SKIP no long test video available")
        return 0

    duration = duration_seconds(video)
    print(f" video : {video.name}")
    print(f" duration : {duration:.0f}s")

    frames = sample_frames(video, DEFAULT_INTERVAL)
    stamps = [ts for ts, _img in frames]
    print(f" sampled : {len(frames)} frames -> {[stamp(t) for t in stamps[:6]]}")

    if not frames:
        print(" FAIL no frames sampled")
        return 1

    # THE CHECK THAT WOULD HAVE CAUGHT THE SEEK BUG.
    #
    # The first seeking version passed microseconds to a stream whose time base
    # was 1/6000, so every seek overshot and 18 of 19 samples came back at 188.9s.
    # It still produced a plausible list, so nothing complained. Even spacing is
    # the property that was actually violated.
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    if gaps:
        uneven = [g for g in gaps if not (DEFAULT_INTERVAL * 0.5 <= g <= DEFAULT_INTERVAL * 1.5)]
        print(f" gaps : min={min(gaps):.1f}s max={max(gaps):.1f}s expected={DEFAULT_INTERVAL:.0f}s")
        if uneven:
            print(f" FAIL {len(uneven)} gaps are not near {DEFAULT_INTERVAL:.0f}s: "
                  f"{[round(g, 1) for g in uneven[:5]]}")
            return 1

    duplicates = len(stamps) - len(set(round(s, 2) for s in stamps))
    if duplicates:
        print(f" FAIL {duplicates} frames share a timestamp")
        return 1

    index = VideoIndex()
    added = index.add_video(1, video, DEFAULT_INTERVAL)
    print(f" embedded : {added} frames")
    if added != len(frames):
        print(f" FAIL sampled {len(frames)} but embedded {added}")
        return 1

    hits = index.search("a screenshot of code", k=3)
    if not hits:
        print(" FAIL search returned nothing")
        return 1
    print(f" search : 'a screenshot of code' -> {[stamp(t) for _d, t, _s in hits]}")

    print()
    print(" PASS frames are evenly spaced, unique, and searchable by description")
    return 0


if __name__ == "__main__":
    sys.exit(main())
