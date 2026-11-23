#!/usr/bin/env bash
# Build a .dmg that can be sent to someone.
#
# The .app alone works, but a bare .app in a zip is a confusing thing to receive:
# macOS quarantines it, the user has to know to drag it to Applications, and the
# first launch shows "unidentified developer" with no context.
#
# A disk image solves the first two: it opens to a window with the app on one side
# and an Applications folder on the other, which is the drag-to-install gesture
# every macOS user already knows.
#
# IT DOES NOT SOLVE THE THIRD. Signing and notarisation need an Apple Developer
# account ($99/year) and credentials that cannot be invented, so this produces an
# UNSIGNED image. The README explains what the user will see and how to get past
# it. Do not describe this as a finished consumer installer until it is signed.
#
# ./bin/make-dmg.sh
#
# Requires: the .app built by bin/make-app.sh, and the daemon built by
# bin/build-sidecar.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$HERE/src-tauri/target/release/device-search.app"
OUT="$HERE/dist"

# THE VERSION COMES FROM VERSION, NOT FROM A COMMIT HASH.
#
# `git describe --tags --always` was used here, and there were no tags - so it fell
# back to the short hash and every installer was named after a commit. Nine of them
# accumulated, all reporting 0.1.0, distinguishable only by a hash nobody reads.
# Tagging is part of ./bin/release.sh; this script just reads the number.
VERSION="$(tr -d '[:space:]' < "$HERE/VERSION" 2>/dev/null || echo "0.0.0")"
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo " VERSION is '$VERSION', which is not MAJOR.MINOR.PATCH" >&2
    exit 1
fi
DMG="$OUT/device-search-$VERSION-macos-arm64.dmg"

if [ ! -d "$APP" ]; then
    echo " no app at $APP" >&2
    echo " build it first: ./bin/make-app.sh" >&2
    exit 1
fi

# Refuse to ship a bundle without the daemon. An app that needs Python and the
# repository it was built in works on the build machine and nowhere else, and the
# failure is a blank window with no message.
if [ ! -x "$APP/Contents/MacOS/device-search-daemon" ]; then
    echo " the bundle has no daemon, so it will only run on this machine" >&2
    echo " build it first: ./bin/build-sidecar.sh && ./bin/make-app.sh" >&2
    exit 1
fi

mkdir -p "$OUT"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "=============================================================="
echo "BUILDING $DMG"
echo "=============================================================="

cp -R "$APP" "$STAGE/"
# The Applications symlink is what makes the window show a drag target.
ln -s /Applications "$STAGE/Applications"

# A note inside the image, because the first thing the user sees after opening it
# is a security dialog that explains nothing.
cat > "$STAGE/READ ME FIRST.txt" <<'TXT'
device-search


TO INSTALL
  Drag the app onto the Applications folder in this window.

FIRST LAUNCH
  macOS will say this app is from an unidentified developer, because it is not
  signed with a paid Apple Developer certificate. To open it anyway:

    right-click the app -> Open -> Open

  You only have to do this once. After that it opens normally.

WHAT IT DOES
  It builds a search index inside ~/.device-search on your own machine.
  Nothing is uploaded anywhere unless you connect a model in Settings.

FIRST RUN
  The app asks which folders to index and shows the time and disk each one
  needs before it starts. Start with a folder you care about rather than your
  whole home directory.
TXT

echo " staging complete"

# THE SIZE IS STATED EXPLICITLY, AND IT HAS TO BE.
#
# Without -size, hdiutil estimates the image size from the source folder and
# failed with "No space left on device" on an 87 MB bundle with 157 GB free. It
# was not the disk: the estimate is for the UNCOMPRESSED contents and UDZO then
# has to write a compressed image that fits inside it. Stating a size larger than
# the source makes the estimate moot.
#
# 50% headroom, because the compressed result is smaller than the source but the
# working image is not.
SRC_MB=$(du -sm "$STAGE" | cut -f1)
IMG_MB=$(( SRC_MB + SRC_MB / 2 + 32 ))

hdiutil create -volname "device-search" \
    -srcfolder "$STAGE" \
    -ov -format UDZO \
    -size "${IMG_MB}m" \
    "$DMG" >/dev/null

SIZE=$(du -sh "$DMG" | cut -f1)
echo " built: $DMG ($SIZE)"
echo
echo " UNSIGNED. A recipient will see 'unidentified developer' and must"
echo " right-click -> Open the first time. Codesigning needs an Apple"
echo " Developer account; the commands are in the README."
echo
echo " verify the contents without installing:"
echo " hdiutil attach '$DMG' && ls /Volumes/device-search"
