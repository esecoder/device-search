"""The version must be the same everywhere it is written.

A number that disagrees with itself is worse than no number: the About box says
0.1.0, the bundle says 0.2.0, and neither identifies the build. This checks the
three places by reading them, which is the only way a mismatch is ever noticed
before someone reports it.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    version_file = ROOT / "VERSION"
    if not version_file.exists():
        print("  FAIL  no VERSION file")
        return 1
    v = version_file.read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        print(f"  FAIL  VERSION is {v!r}, not MAJOR.MINOR.PATCH")
        return 1
    print(f"  VERSION file      : {v}")

    seen = {}

    init = (ROOT / "device_search" / "__init__.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    seen["__init__.py"] = m.group(1) if m else "(missing)"

    conf = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text())
    seen["tauri.conf.json"] = conf.get("version", "(missing)")

    plist = (ROOT / "bin" / "make-app.sh").read_text()
    m = re.search(r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>", plist)
    seen["Info.plist"] = m.group(1) if m else "(missing)"

    bad = []
    for where, got in seen.items():
        ok = got == v
        print(f"  {where:<18}: {got}  {'OK' if ok else '<- MISMATCH'}")
        if not ok:
            bad.append(where)

    print()
    if bad:
        print(f"  FAIL  {', '.join(bad)} disagrees with VERSION")
        print("        run ./bin/release.sh, which syncs all three")
        return 1
    print(f"  PASS  every version string is {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
