#!/usr/bin/env python3
"""
news-triage.py — Serves the editorial triage interface on localhost.

Reads candidates.json, serves triage.html, saves decisions to triage.json.

Usage:
    python news-triage.py              # serve on port 8080
    python news-triage.py --port 9000  # custom port

Stdlib only. No pip installs.
"""

import http.server
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CANDIDATES = SCRIPT_DIR / "candidates.json"
TRIAGE_OUT = SCRIPT_DIR / "triage.json"
TRIAGE_HTML = SCRIPT_DIR / "triage.html"
PORT = 8080


class TriageHandler(http.server.BaseHTTPRequestHandler):
    """Handle GET for pages/data and POST for saving decisions."""

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_file(TRIAGE_HTML, "text/html")
        elif self.path == "/api/candidates":
            self._serve_file(CANDIDATES, "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                TRIAGE_OUT.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
                print(f"  Saved triage decisions to {TRIAGE_OUT}")
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f'{{"error": "{exc}"}}'.encode())
        else:
            self.send_error(404)

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(404, f"File not found: {path.name}")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        # Quieter logging — only show non-200 or POST
        status = args[1] if len(args) > 1 else ""
        if "POST" in str(args[0]) or str(status) != "200":
            super().log_message(format, *args)


def main():
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])

    if not CANDIDATES.exists():
        print(f"ERROR: {CANDIDATES} not found.")
        print("Run /news find first to generate candidate stories.")
        sys.exit(1)

    server = http.server.HTTPServer(("127.0.0.1", port), TriageHandler)
    print(f"Triage server running at http://localhost:{port}")
    print(f"Reading from: {CANDIDATES}")
    print(f"Will save to: {TRIAGE_OUT}")
    print("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
