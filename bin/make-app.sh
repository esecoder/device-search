#!/usr/bin/env bash
# make-app.sh — wrap the binary in a real macOS .app bundle.
#
# ⚠️⚠️ WHY THIS IS NOT COSMETIC, AND WHY THE UI DID NOT APPEAR WITHOUT IT
#
# `cargo build --release` produces a bare executable. Double-clicking it, or running it from a
# terminal, does NOT make it a macOS application. It has:
#     * no Info.plist, so no bundle identity for the window server to register
#     * no declared package type, so LaunchServices does not treat it as an app
#     * no NSHighResolutionCapable, so the window renders at 1x and looks blurry on Retina
#
# ⚠️ The observable symptoms were exactly this: a dock icon and a title bar, no menu-bar (tray)
# item, and a global hotkey that appeared to do nothing. **A menu-bar item is an NSStatusItem,
# and an unbundled binary cannot reliably create one.** No amount of Rust code fixes that; the
# bundle is the missing piece.
#
# ⚠️ AND NOTE WHAT THIS IS NOT: it does not fix the global hotkey needing Accessibility
# permission, because it does not need it. `global-hotkey` on macOS calls `RegisterEventHotKey`
# (the Carbon hotkey API), which requires no permission at all. Only CGEventTap-based key
# *monitoring* does. The earlier claim that Accessibility was required was wrong.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$HERE/src-tauri/target/release/device-search"
APP="$HERE/src-tauri/target/release/device-search.app"
ICON_PNG="$HERE/src-tauri/icons/icon.png"

[ -x "$BIN" ] || { echo "  ✗ build first:  cd src-tauri && cargo build --release"; exit 1; }

# ⚠️⚠️ KILL THE RUNNING INSTANCE, BECAUSE `open` DOES NOT RELAUNCH.
#
# The single most expensive mistake in this project's testing: on macOS, `open App.app` when the
# app is ALREADY RUNNING just brings it to the front. It does not restart it, and it does not
# warn. So a rebuild followed by `open` looks exactly like a successful update while the user
# goes on interacting with the OLD process.
#
# ⚠️ Measured cost of not knowing this: one instance ran for 28 HOURS across seven rebuilds.
# Every fix was reported as "still not working" because none of them had ever been loaded.
#
# ⚠️ AND THE DAEMON IS PYTHON, READ FROM THE REPO AT RUNTIME — so it picks up changes only when
# it RESTARTS. A stale daemon serves stale code just as convincingly as a stale binary.
if pgrep -f "device-search.app/Contents/MacOS/device-search" >/dev/null 2>&1 \
   || pgrep -f "release/device-search" >/dev/null 2>&1; then
    echo "  quitting the running instance (open does not relaunch an app)"
    pkill -f "device-search.app/Contents/MacOS/device-search" 2>/dev/null || true
    pkill -f "release/device-search" 2>/dev/null || true
    pkill -f "device_search.server" 2>/dev/null || true
    sleep 2
fi

# ⚠️⚠️ FORCE THE TAURI ASSET CODEGEN TO RERUN, AND THIS IS NOT A WORKAROUND.
#
# The UI is EMBEDDED IN THE BINARY at compile time (frontendDist = ../ui, brotli-compressed
# into a content-addressed cache under target/). But cargo does NOT treat ui/*.js as an input
# to that codegen: change ONLY the interface and `cargo build` reports "Finished" while
# re-embedding NOTHING.
#
# ⚠️ MEASURED, and it is why a day of UI fixes appeared to do nothing:
#     current ui/setup.js       18,987 bytes, contains the new code
#     newest embedded asset      4,089 bytes, contains none of it
#
# Every fix was written, committed, tested and rebuilt — and the app kept serving an interface
# from hours earlier. Worse than the `open` trap, because that one at least showed a stale
# BINARY; this shows a current binary containing a stale UI.
#
# ⚠️ TOUCHING A RUST INPUT IS THE RELIABLE WAY. Deleting the cache also works but the cache is
# keyed by content hash, so it is shared across builds and removing it is not obviously safe.
touch "$HERE/src-tauri/src/main.rs"

echo "  building device-search.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ---- the executable ----------------------------------------------------------
cp "$BIN" "$APP/Contents/MacOS/device-search"
chmod +x "$APP/Contents/MacOS/device-search"

# ---- the icon ----------------------------------------------------------------
# ⚠️ .icns, NOT .png. macOS reads CFBundleIconFile as an .icns resource; a png silently produces
# the generic blank icon, which reads as "this app is broken" before it is even opened.
if [ -f "$ICON_PNG" ]; then
    ICONSET="$(mktemp -d)/icon.iconset"
    mkdir -p "$ICONSET"
    for size in 16 32 64 128 256 512; do
        sips -z $size $size "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1
        d=$((size * 2))
        sips -z $d $d "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns" 2>/dev/null \
        && echo "    icon: icon.icns" \
        || echo "    ⚠️ could not build .icns — the app will use the generic icon"
fi

# ---- Info.plist --------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>       <string>device-search</string>
    <key>CFBundleIdentifier</key>       <string>dev.devicesearch.app</string>
    <key>CFBundleName</key>             <string>device-search</string>
    <key>CFBundleDisplayName</key>      <string>device-search</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key>          <string>0.1.0</string>
    <key>CFBundleIconFile</key>         <string>icon</string>
    <key>LSMinimumSystemVersion</key>   <string>10.15</string>

    <!-- ⚠️ WITHOUT THIS THE WINDOW RENDERS AT 1x. On a Retina display that means visibly
         blurry text — the kind of wrongness a user notices immediately and cannot name. -->
    <key>NSHighResolutionCapable</key>  <true/>

    <!-- ⚠️ false = a normal dock app. Set true for a menu-bar-only app with no dock icon.
         Left false deliberately: a first-run app the user cannot find in the dock is the exact
         problem this bundle was built to fix. -->
    <key>LSUIElement</key>              <false/>

    <!-- ⚠️⚠️ APP TRANSPORT SECURITY, AND ITS ABSENCE IS THE WHOLE BUG.
         macOS BLOCKS PLAIN-HTTP REQUESTS from a WKWebView by default. The UI fetches
         http://127.0.0.1:8734/api/health, which ATS refuses — so the app reported "the search
         engine did not start" while the daemon was RUNNING, LISTENING, and answering curl
         perfectly. ⚠️ ATS applies to the APP, not to the shell, which is why testing with curl
         proved nothing was wrong.

         ⚠️ `NSAllowsLocalNetworking` IS THE RIGHT KEY, not `NSAllowsArbitraryLoads`.
         It permits loopback and .local connections while KEEPING ATS protection for everything
         on the internet. Using ArbitraryLoads would fix this by disabling the protection
         entirely — the kind of fix that works and quietly removes a security property. -->
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key><true/>
    </dict>

    <key>NSHumanReadableCopyright</key> <string>open source</string>
</dict>
</plist>
PLIST

# ---- validate before declaring success ---------------------------------------
# ⚠️ `plutil -lint` because a malformed plist makes macOS silently refuse to launch the app with
# no useful error — the worst possible failure to debug from the outside.
if ! plutil -lint "$APP/Contents/Info.plist" >/dev/null 2>&1; then
    echo "  ✗ Info.plist is malformed"; exit 1
fi

echo "    executable: $(du -h "$APP/Contents/MacOS/device-search" | cut -f1)"
# ⚠️ VERIFIED, NOT ASSUMED. The build succeeding says nothing about whether the CURRENT ui/ is
# inside the binary — that is the whole lesson above. This decompresses the embedded assets and
# checks that the newest one matches the file on disk.
node "$HERE/check-assets.js" || {
    echo "  ✗ the embedded UI does not match ui/ — the app would serve stale code"
    exit 1
}

echo "  ✅ $APP"
echo
echo "  run it:   open '$APP'"
echo "  or:       '$APP/Contents/MacOS/device-search'"
echo
echo "  ⚠️ This script now quits any running instance first. If you open the app"
echo "     WITHOUT rebuilding, and it is already running, macOS will NOT relaunch it."
echo
echo "  ⚠️ FIRST LAUNCH: if macOS says the app is from an unidentified developer,"
echo "     right-click the app and choose Open, or allow it in System Settings → Privacy."
