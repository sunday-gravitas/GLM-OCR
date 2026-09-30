#!/usr/bin/env python3
"""Push a bootstrap/pipeline stage marker to Drive for remote monitoring.

Stdlib-only (runs before pip dependencies are installed). Reads the local
.drive-creds.json handoff and uploads/updates bootstrap_status.json inside
the ocr-book folder, so an operator can watch provisioning progress from
Google Drive without access to the codespace.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def die(msg: str) -> None:
    print(f"[status_marker] {msg}", file=sys.stderr)


def refresh(creds: dict) -> str:
    data = "&".join(
        f"{k}={quote(v)}"
        for k, v in {
            "grant_type": "refresh_token",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
        }.items()
    ).encode()
    req = urllib.request.Request(
        creds.get("token_uri", "https://oauth2.googleapis.com/token"), data=data
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["access_token"]


def api(method: str, url: str, token: str, data: bytes | None = None,
        content_type: str | None = None):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--logs", action="store_true",
                    help="append tails of /tmp/pipeline.log and "
                         "/tmp/pipeline-bootstrap.log")
    ap.add_argument("--state", action="store_true",
                    help="embed /tmp/pipeline_state.json")
    args = ap.parse_args()

    creds_path = Path(__file__).resolve().parent.parent / ".drive-creds.json"
    if not creds_path.is_file():
        die("no .drive-creds.json — skipping marker upload")
        return 0  # non-fatal

    try:
        creds = json.loads(creds_path.read_text())
        token = refresh(creds)
    except Exception as exc:  # noqa: BLE001
        die(f"auth failed: {exc}")
        return 0

    ocr_book_folder = "1iDlabXG8aF9zxSyB7EzlOboL2N-buNqj"  # THSC/level11/physics/ocr-book
    file_name = "bootstrap_status.json"
    payload = {
        "component": "codespace-bootstrap",
        "codespace": __import__("os").environ.get("CODESPACE_NAME", "?"),
        "status": args.status,
        "note": args.note,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if args.logs:
        for label, path in (
            ("pipeline_log_tail", "/tmp/pipeline.log"),
            ("bootstrap_log_tail", "/tmp/pipeline-bootstrap.log"),
            ("supervisor_log_tail", "/tmp/supervisor.log"),
            ("ollama_log_tail", "/tmp/ollama.log"),
        ):
            try:
                text = Path(path).read_text(errors="replace").splitlines()[-60:]
                payload[label] = "\n".join(text)
            except OSError:
                payload[label] = "(not available)"
    if args.state:
        try:
            payload["pipeline_state"] = json.loads(
                Path("/tmp/pipeline_state.json").read_text())
        except (OSError, ValueError):
            payload["pipeline_state"] = "(not available)"
    body = json.dumps(payload, indent=2).encode()

    # find existing marker file (update-in-place keeps a single status file)
    q = quote(
        f"'{ocr_book_folder}' in parents and name = '{file_name}' "
        f"and trashed = false"
    )
    status, resp = api(
        "GET",
        "https://www.googleapis.com/drive/v3/files"
        f"?q={q}&fields=files(id)",
        token,
    )
    file_id = None
    if status == 200:
        files = json.loads(resp).get("files", [])
        if files:
            file_id = files[0]["id"]

    if file_id:
        status, resp = api(
            "PATCH",
            f"https://www.googleapis.com/upload/drive/v3/files/{file_id}"
            "?uploadType=media",
            token, body, "application/json",
        )
    else:
        boundary = "marker-951"
        meta = json.dumps({"name": file_name, "parents": [ocr_book_folder]})
        payload_mp = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{meta}\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n"
        ).encode() + body + f"\r\n--{boundary}--".encode()
        status, resp = api(
            "POST",
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=multipart&fields=id",
            token, payload_mp,
            f"multipart/related; boundary={boundary}",
        )
    if status >= 300:
        die(f"upload failed: {status} {resp.decode()[:200]}")
        return 0
    print(f"[status_marker] {args.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
