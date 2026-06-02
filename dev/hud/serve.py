"""No-cache static file server for the HUD harness.

Plain http.server caches panel.css/panel.js, so edits don't show on
reload. This sends Cache-Control: no-store on every response so each
Playwright navigation re-fetches the latest files. Run from the repo
root:  python3 dev/hud/serve.py [port]
"""
import http.server
import socketserver
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8771


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()


with socketserver.TCPServer(("127.0.0.1", PORT), NoCacheHandler) as httpd:
    print(f"serving repo at http://127.0.0.1:{PORT} (no-store)")
    httpd.serve_forever()
