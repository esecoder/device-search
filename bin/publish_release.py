#!/usr/bin/env python3
"""Attach the installer to a GitHub Release, using only the standard library.

`gh` is the pleasant way to do this and the release script prefers it. This exists
for the machine that has a token and no gh, which is the common case for a
developer who has never needed one.

    GITHUB_TOKEN=... ./bin/publish_release.py 0.1.0 dist/x.dmg dist/y.txt

WHY A RELEASE AND NOT THE REPOSITORY

An 85 MB binary committed to a git repository is fetched by every clone, forever,
in every branch, and cannot be removed without rewriting history - which is exactly
the 840 MB mistake this project already made once. A release asset is stored
separately, has a stable URL, and is tied to the tag.

IDEMPOTENT

Running it twice does not create two releases. The release for the tag is looked up
first and created only if absent, and an asset that already exists is replaced
rather than duplicated - because a release step that only works once is a release
step that fails the second time you need it.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "esecoder/device-search"
API = "https://api.github.com"


def _ssl_ctx():
    """The certifi bundle, because python.org builds ship without one.

    Measured here: bare urlopen raised CERTIFICATE_VERIFY_FAILED on the GitHub API
    - "unable to get local issuer certificate". The same failure hit the model
    client and the downloader earlier in this project; it is a property of the
    interpreter, not of the endpoint.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _req(method: str, url: str, token: str, data: bytes | None = None,
         content_type: str = "application/json") -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=120, context=_ssl_ctx()) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def main() -> int:
    if len(sys.argv) < 3:
        print("  usage: publish_release.py VERSION FILE [FILE...]")
        return 2
    version, files = sys.argv[1], [Path(f) for f in sys.argv[2:]]
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("  no GITHUB_TOKEN in the environment")
        return 1
    for f in files:
        if not f.exists():
            print(f"  missing: {f}")
            return 1

    tag = f"v{version}"

    # 1. find or create the release
    status, rel = _req("GET", f"{API}/repos/{REPO}/releases/tags/{tag}", token)
    if status == 404:
        notes = ""
        changelog = Path("CHANGELOG.md")
        if changelog.exists():
            notes = changelog.read_text(encoding="utf-8")[:20000]
        status, rel = _req(
            "POST", f"{API}/repos/{REPO}/releases", token,
            json.dumps({"tag_name": tag, "name": f"DeviceSearch {version}",
                        "body": notes, "draft": False, "prerelease": False}).encode())
        if status not in (200, 201):
            print(f"  could not create the release: HTTP {status} {rel.get('message','')}")
            return 1
        print(f"  created release {tag}")
    elif status == 200:
        print(f"  release {tag} already exists")
    else:
        print(f"  could not look up the release: HTTP {status} {rel.get('message','')}")
        return 1

    upload_url = rel.get("upload_url", "").split("{")[0]
    existing = {a["name"]: a["id"] for a in rel.get("assets", [])}

    # 2. upload each file, replacing one that is already there
    for path in files:
        if path.name in existing:
            _req("DELETE", f"{API}/repos/{REPO}/releases/assets/{existing[path.name]}", token)
            print(f"  replaced existing asset {path.name}")
        data = path.read_bytes()
        status, out = _req("POST", f"{upload_url}?name={path.name}", token,
                           data, content_type="application/octet-stream")
        if status not in (200, 201):
            print(f"  upload of {path.name} failed: HTTP {status} {out.get('message','')}")
            return 1
        print(f"  uploaded {path.name} ({len(data)/1e6:.0f} MB)")

    print(f"  https://github.com/{REPO}/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
