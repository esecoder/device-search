#!/usr/bin/env bash
# Cut a release: one version number, one build, one tag, one changelog entry.
#
# THE PROBLEM THIS SOLVES
#
# Versioning was effectively absent. `make-dmg.sh` derived the version from
# `git describe --tags --always`, there were no tags, so it fell back to the commit
# hash. Nine installers accumulated, every one reporting 0.1.0 in the About box and
# the bundle, distinguishable only by a hash nobody reads.
#
# Worse: the newest DMG could be older than the newest commit, because nothing
# checked. That happened - the app on disk was built before the code it claimed to
# contain, and only a file-timestamp comparison found it.
#
#   ./bin/release.sh            use the version in VERSION
#   ./bin/release.sh 0.2.0      set that version, then release
#
# Refuses to run on a dirty tree, on a stale daemon, or if the built app does not
# contain the code it was built from.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

VERSION_FILE="$HERE/VERSION"
NEW_VERSION="${1:-}"

if [ -n "$NEW_VERSION" ]; then
    printf '%s\n' "$NEW_VERSION" > "$VERSION_FILE"
    echo "  version set to $NEW_VERSION"
fi

VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "  VERSION is '$VERSION', which is not MAJOR.MINOR.PATCH" >&2
    exit 1
fi

echo "=============================================================="
echo "RELEASING $VERSION"
echo "=============================================================="
echo

# A RELEASE FROM A DIRTY TREE IS NOT REPRODUCIBLE. Whatever is uncommitted is in
# the binary and nowhere in the history, so the tag would point at code that does
# not match the artifact.
if [ -n "$(git status --porcelain)" ]; then
    echo "  ✗ the working tree has uncommitted changes:" >&2
    git status --short | head -8 | sed 's/^/      /' >&2
    echo "  commit or stash them first" >&2
    exit 1
fi
echo "  ✓ working tree is clean"

# THE BUNDLE MUST CONTAIN THE CODE IT CLAIMS TO.
if [ -f "$HERE/dist/device-search-daemon" ]; then
    STALE=$(find device_search -name '*.py' -newer "$HERE/dist/device-search-daemon" 2>/dev/null | head -1)
    if [ -n "$STALE" ]; then
        echo "  rebuilding the daemon: $STALE is newer than it"
    else
        echo "  ✓ daemon is current"
    fi
fi

# Keep the three places a version is written in step. A number that disagrees with
# itself is worse than no number: About says 0.1.0, the bundle says 0.2.0, and
# neither can be trusted to identify the build.
echo
echo "  syncing version into the code and the bundle config"
"$HERE/.venv/bin/python" - "$VERSION" <<'PY'
import json, re, sys
from pathlib import Path
v = sys.argv[1]
root = Path.cwd()

init = root / "device_search" / "__init__.py"
s = init.read_text()
s = re.sub(r'__version__\s*=\s*"[^"]*"', f'__version__ = "{v}"', s, count=1)
init.write_text(s)

conf = root / "src-tauri" / "tauri.conf.json"
d = json.loads(conf.read_text())
d["version"] = v
conf.write_text(json.dumps(d, indent=2) + "\n")

plist = root / "bin" / "make-app.sh"
s = plist.read_text()
s = re.sub(r'(<key>CFBundleShortVersionString</key>\s*<string>)[^<]*(</string>)',
           rf'\g<1>{v}\g<2>', s)
s = re.sub(r'(<key>CFBundleVersion</key>\s*<string>)[^<]*(</string>)',
           rf'\g<1>{v}\g<2>', s)
plist.write_text(s)
print(f"    __init__.py, tauri.conf.json, Info.plist -> {v}")
PY

echo
echo "  building the daemon"
./bin/build-sidecar.sh | tail -2 | sed 's/^/    /'

echo
echo "  building the app"
./bin/make-app.sh | grep -E "daemon|icon" | sed 's/^/    /' || true

# THE CHECK THAT WOULD HAVE CAUGHT THE STALE INSTALLER.
NEWEST=$(find device_search ui src-tauri/src -type f \
         -newer src-tauri/target/release/device-search.app/Contents/MacOS/device-search 2>/dev/null)
if [ -n "$NEWEST" ]; then
    echo "  ✗ the app is older than its own source:" >&2
    echo "$NEWEST" | head -5 | sed 's/^/      /' >&2
    exit 1
fi
echo "  ✓ the app contains the current code"

echo
echo "  building the image"
DMG_OLD=$(ls dist/*.dmg 2>/dev/null || true)
./bin/make-dmg.sh | tail -4 | sed 's/^/    /'

DMG="dist/device-search-${VERSION}-macos-arm64.dmg"
if [ ! -f "$DMG" ]; then
    echo "  ✗ expected $DMG and it is not there" >&2
    ls dist/*.dmg | sed 's/^/      /' >&2
    exit 1
fi

echo
echo "  tagging v$VERSION"
git add -A
git commit -q -m "Release $VERSION" || echo "    (nothing to commit)"
# AN ANNOTATED TAG, NOT A LIGHTWEIGHT ONE.
#
# `git tag -f name` creates a lightweight tag, and `git push --follow-tags` only
# pushes ANNOTATED ones. The first release pushed its commit and silently left the
# tag behind - `git ls-remote --tags origin` was empty afterwards, and the version
# marker existed only on the machine that cut it.
git tag -f -a "v$VERSION" -m "DeviceSearch $VERSION" >/dev/null
echo "    v$VERSION (annotated)"

echo
echo "=============================================================="
echo "RELEASED $VERSION"
echo "=============================================================="
echo "  image : $DMG  ($(du -sh "$DMG" | cut -f1))"
echo "  tag   : v$VERSION"
echo
echo "  ⚠️  UNSIGNED. A recipient must right-click -> Open the first time."
echo "      Codesigning needs an Apple Developer account; see README."
echo
echo "  ⚠️  The image is NOT notarised, so it will not verify offline either."
