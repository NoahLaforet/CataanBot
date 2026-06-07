#!/usr/bin/env python3
"""One-time: obtain a Chrome Web Store API refresh token via the loopback
OAuth flow, so scripts/cws_publish.py can upload + publish releases for you.

Prereqs (see docs/CWS_API.md for the click-by-click):
  1. A Google Cloud project with the "Chrome Web Store API" enabled.
  2. An OAuth client of type "Desktop app" (its client id + secret).
  3. Yourself added as a test user on the OAuth consent screen.
  4. CWS_CLIENT_ID and CWS_CLIENT_SECRET filled into the gitignored .env.

Run it:
  PYTHONPATH=src .venv/bin/python scripts/cws_get_refresh_token.py
It opens a Google consent page, captures the redirect on localhost, and
prints the refresh token. Paste that into .env as CWS_REFRESH_TOKEN.

Stdlib only, no network creds leave your machine except the standard
OAuth exchange with Google.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/chromewebstore"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _load_env() -> dict[str, str]:
    """Read KEY=VALUE lines from the repo-root .env (gitignored)."""
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    env = _load_env()
    client_id = os.environ.get("CWS_CLIENT_ID") or env.get("CWS_CLIENT_ID")
    client_secret = (os.environ.get("CWS_CLIENT_SECRET")
                     or env.get("CWS_CLIENT_SECRET"))
    if not client_id or not client_secret:
        print("Set CWS_CLIENT_ID and CWS_CLIENT_SECRET in .env first "
              "(see docs/CWS_API.md).", file=sys.stderr)
        return 1

    port = _free_port()
    redirect_uri = f"http://localhost:{port}/"
    captured: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            captured["code"] = (params.get("code") or [""])[0]
            captured["error"] = (params.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = ("Chrome Web Store auth captured. You can close this tab "
                   "and return to the terminal.")
            self.wfile.write(f"<html><body><p>{msg}</p></body></html>"
                             .encode())
            done.set()

        def log_message(self, *args):  # silence the default access log
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
    auth_url = f"{AUTH_URL}?{auth_params}"
    print("Opening the Google consent page in your browser...")
    print("If it does not open, paste this URL manually:\n")
    print(auth_url, "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001
        pass

    if not done.wait(timeout=300):
        print("Timed out waiting for the OAuth redirect.", file=sys.stderr)
        return 1
    server.shutdown()

    if captured.get("error") or not captured.get("code"):
        print(f"OAuth failed: {captured.get('error') or 'no code returned'}",
              file=sys.stderr)
        return 1

    data = urllib.parse.urlencode({
        "code": captured["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:  # noqa: F821
        print(f"Token exchange failed: {e.read().decode()}", file=sys.stderr)
        return 1

    refresh = tok.get("refresh_token")
    if not refresh:
        print("No refresh_token returned. Re-run with a fresh consent "
              "(the script already requests prompt=consent).", file=sys.stderr)
        print(json.dumps(tok, indent=2), file=sys.stderr)
        return 1

    print("\nSuccess. Add this line to your .env:\n")
    print(f"CWS_REFRESH_TOKEN={refresh}\n")
    print("Then publish a release with scripts/cws_publish.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
