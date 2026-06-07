#!/usr/bin/env python3
"""Upload + publish the CatanBot extension to the Chrome Web Store via the
CWS API, so a release no longer needs a manual dev-console upload.

The web dev console cannot be automated (Chrome blocks scripting the
extensions gallery), but the CWS API is not blocked. This script refreshes
an access token from the stored OAuth creds, uploads the packaged zip as a
new draft, and submits it for review.

Setup once (see docs/CWS_API.md): fill CWS_CLIENT_ID, CWS_CLIENT_SECRET,
CWS_REFRESH_TOKEN, CWS_ITEM_ID into the gitignored .env (run
scripts/cws_get_refresh_token.py for the refresh token).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/cws_publish.py [--zip PATH] \
      [--no-publish]
Defaults to dist/catanbot-extension-v{manifest version}.zip. --no-publish
uploads the draft but leaves submission to you (handy when a prior version
is still in review, which blocks a new submission).

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/chromewebstore/v1.1/items/{id}"
PUBLISH_URL = "https://www.googleapis.com/chromewebstore/v1.1/items/{id}/publish"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _manifest_version() -> str:
    data = json.loads((ROOT / "extension" / "manifest.json").read_text())
    return str(data.get("version", "0.0.0"))


def _access_token(env: dict[str, str]) -> str:
    for key in ("CWS_CLIENT_ID", "CWS_CLIENT_SECRET", "CWS_REFRESH_TOKEN"):
        if not env.get(key):
            raise SystemExit(
                f"{key} missing from .env (see docs/CWS_API.md).")
    body = urllib.parse.urlencode({
        "client_id": env["CWS_CLIENT_ID"],
        "client_secret": env["CWS_CLIENT_SECRET"],
        "refresh_token": env["CWS_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"token refresh failed: {e.read().decode()}")
    if "access_token" not in tok:
        raise SystemExit(f"no access_token: {tok}")
    return tok["access_token"]


def _api(url: str, token: str, *, method: str, data: bytes | None = None,
         content_type: str | None = None) -> dict:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("x-goog-api-version", "2")
    if content_type:
        req.add_header("Content-Type", content_type)
    if data is None and method == "POST":
        req.add_header("Content-Length", "0")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=None, help="extension zip to upload")
    ap.add_argument("--no-publish", action="store_true",
                    help="upload the draft but do not submit for review")
    args = ap.parse_args()

    env = _load_env()
    item_id = env.get("CWS_ITEM_ID")
    if not item_id:
        raise SystemExit("CWS_ITEM_ID missing from .env (the store item id).")

    version = _manifest_version()
    zip_path = Path(args.zip) if args.zip else (
        ROOT / "dist" / f"catanbot-extension-v{version}.zip")
    if not zip_path.exists():
        raise SystemExit(
            f"{zip_path} not found. Run bin/build-extension-zip.sh first.")

    print(f"item   {item_id}")
    print(f"zip    {zip_path}  (v{version})")
    token = _access_token(env)

    print("==> upload new package")
    blob = zip_path.read_bytes()
    up = _api(UPLOAD_URL.format(id=item_id), token, method="PUT", data=blob,
              content_type="application/zip")
    if up.get("_http_error"):
        print(f"upload HTTP {up['_http_error']}: {up['_body']}",
              file=sys.stderr)
        return 1
    state = up.get("uploadState")
    print(f"    uploadState = {state}")
    if state not in ("SUCCESS", "IN_PROGRESS"):
        print(json.dumps(up.get("itemError") or up, indent=2),
              file=sys.stderr)
        return 1

    if args.no_publish:
        print("draft uploaded; skipping publish (--no-publish).")
        return 0

    print("==> publish (submit for review)")
    pub = _api(PUBLISH_URL.format(id=item_id), token, method="POST")
    if pub.get("_http_error"):
        print(f"publish HTTP {pub['_http_error']}: {pub['_body']}",
              file=sys.stderr)
        # A common, non-fatal case: a prior version is still in review.
        if "PENDING" in (pub.get("_body") or "").upper():
            print("A previous submission is still in review; re-run "
                  "publish once it clears, or upload with --no-publish.")
        return 1
    status = pub.get("status") or []
    print(f"    status = {status}")
    detail = pub.get("statusDetail")
    if detail:
        print(f"    detail = {detail}")
    ok = any(s in ("OK", "PUBLISHED", "ITEM_PENDING_REVIEW") for s in status)
    print("done." if ok else "publish returned an unexpected status.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
