#!/usr/bin/env python3
"""Standalone status server + keep-alive for the OCR pipeline.

Runs in its OWN process (never inside the pipeline) — binding the port from
the pipeline process repeatedly coincided with that process losing outbound
connectivity, so the pipeline now only writes plain JSON state to
/tmp/pipeline_state.json and this little server exposes it.

  GET /status  → pipeline state JSON (or {"status": "no-state-yet"})
  GET /healthz → "ok"

Also self-pings the codespace's public forwarded URL every 10 minutes so
the idle auto-stop timer resets while the pipeline works (requests through
a codespace's forwarded port count as activity).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE_FILE = Path("/tmp/pipeline_state.json")
PORT = int(os.environ.get("OCR_STATUS_PORT", "8787"))
SELF_PING = os.environ.get("OCR_SELF_PING", "1") == "1"
PING_INTERVAL = int(os.environ.get("OCR_HEARTBEAT_SEC", "300"))


def read_state() -> bytes:
    try:
        return STATE_FILE.read_bytes()
    except OSError:
        return json.dumps({"status": "no-state-yet",
                           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       time.gmtime())}).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/status", "/healthz"):
            body = read_state() if self.path != "/healthz" else b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # quiet
        pass


def self_ping() -> None:
    name = os.environ.get("CODESPACE_NAME", "").strip()
    domain = os.environ.get(
        "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev").strip()
    if not name:
        return
    url = f"https://{name}-{PORT}.{domain}/status"
    while True:
        time.sleep(PING_INTERVAL)
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=30) as resp:
                resp.read()
        except OSError:
            pass


def main() -> int:
    if SELF_PING:
        threading.Thread(target=self_ping, daemon=True).start()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        print(f"status server: cannot bind :{PORT}: {exc}", file=sys.stderr)
        return 0
    print(f"status server listening on :{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
