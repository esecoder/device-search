#!/usr/bin/env bash
# Build a self-contained daemon executable.
#
# The Tauri app today spawns `python -m device_search.server` from this repository.
# That works on the machine it was compiled on and nowhere else: the path is baked
# in at compile time and the Python packages are not in the bundle. This produces a
# single file that contains the interpreter, the packages and the code, so the app
# can find it inside its own bundle and run on a machine with no Python installed.
#
#   ./bin/build-sidecar.sh
#
# Output: dist/device-search-daemon  (~250-350 MB)
#
# The size is the interpreter plus onnxruntime (64 MB) plus numpy (51 MB) plus the
# rest. It is the price of not requiring the user to install anything.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PY="${PYTHON:-$HERE/.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "  no Python at $PY" >&2
    echo "  create the environment first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-media.txt" >&2
    exit 1
fi

echo "=============================================================="
echo "BUILDING THE DAEMON EXECUTABLE"
echo "=============================================================="
echo

# The entry point is a tiny script rather than the module, because PyInstaller needs
# a real file to analyse. It only calls the same main() the module does.
ENTRY="$(mktemp -t ds_entry).py"
trap 'rm -f "$ENTRY"' EXIT
cat > "$ENTRY" <<'PYEOF'
import multiprocessing
import sys

if __name__ == "__main__":
    # Required when freezing: without it, a frozen process that starts a child
    # re-runs the parent instead of the child.
    multiprocessing.freeze_support()

    # One binary, two jobs. With no arguments it is the daemon; with "index" it is
    # the indexer. The daemon re-runs the indexer as a child process when it needs
    # to repair the index, and in a frozen build the child command is this same
    # executable - so it has to understand the word the daemon passes it.
    if len(sys.argv) > 1 and sys.argv[1] == "index":
        from device_search.cli import main as cli_main
        raise SystemExit(cli_main(["index", "--no-semantic"]))
    from device_search.server import main
    raise SystemExit(main())
PYEOF

echo "  entry point: $ENTRY"
echo "  output     : dist/device-search-daemon"
echo

"$PY" -m PyInstaller \
    --name device-search-daemon \
    --onefile \
    --noconfirm \
    --clean \
    --log-level WARN \
    --hidden-import device_search \
    --hidden-import device_search.server \
    --hidden-import device_search.cli \
    --hidden-import device_search.store \
    --hidden-import device_search.semantic \
    --hidden-import device_search.vectors \
    --hidden-import device_search.crawl \
    --hidden-import device_search.extract \
    --hidden-import device_search.agent \
    --hidden-import device_search.metadata \
    --hidden-import device_search.runtime \
    --collect-data fastembed \
    --collect-data onnxruntime \
    --collect-binaries onnxruntime \
    --collect-submodules pdftext \
    --collect-binaries pypdfium2_raw \
    --paths "$HERE" \
    "$ENTRY"

echo
if [ -x dist/device-search-daemon ]; then
    SIZE=$(du -sh dist/device-search-daemon | cut -f1)
    echo "  built: dist/device-search-daemon  ($SIZE)"
    echo
    echo "  verifying it runs with no repository on the path..."
    cd /tmp
    # `--help` proves the interpreter and the package are inside the binary: if
    # either were missing, argparse would never be reached. An earlier version
    # of this check used `--version`, which the daemon does not define, so it
    # reported failure for a build that was fine.
    if "$HERE/dist/device-search-daemon" --help >/dev/null 2>&1; then
        echo "  the binary starts on its own"
    else
        echo "  the binary did not start; check the log above" >&2
    fi
else
    echo "  build failed: dist/device-search-daemon was not produced" >&2
    exit 1
fi
