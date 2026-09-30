#!/usr/bin/env python3
"""Create a short-lived Drive handoff file for pipeline credentials.

Runs OUTSIDE the codespace (e.g. on the operator's machine or an agent
sandbox): uploads the Drive OAuth credentials as a Drive file, makes it
readable "anyone with link", and writes its public URL into
pipeline/creds_url.txt. The codespace's fetch-drive-creds.sh downloads the
file and immediately deletes it from Drive (burn after reading).

Usage:
    python3 pipeline/make_handoff.py --creds drive_creds.json \
        [--parent <drive-folder-id>]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path


def refresh(creds: dict) -> str:
    data = {
        "grant_type": "refresh_token",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
    }
    req = urllib.request.Request(creds.get("token_uri",
                              "https://oauth2.googleapis.com/token"),
                                 data="&".join(f"{k}={v}" for k, v in data.items()).encode())
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["access_token"]


def api(method: str, url: str, token: str, data: bytes | None = None,
        content_type: str | None = None) -> tuple[int, bytes]:
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
    ap.add_argument("--creds", required=True, help="credentials JSON file")
    ap.add_argument("--parent", default=None,
                    help="Drive folder id for the handoff file")
    args = ap.parse_args()

    creds = json.loads(Path(args.creds).read_text())
    for key in ("client_id", "client_secret", "refresh_token"):
        if not creds.get(key):
            sys.exit(f"credentials missing '{key}'")

    token = refresh(creds)
    body = json.dumps(creds).encode()

    # simple media upload with optional parent folder
    if args.parent:
        boundary = "handoff-951"
        meta = json.dumps({"name": "pipeline_handoff.json",
                           "parents": [args.parent]})
        payload = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{meta}\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n"
        ).encode() + body + f"\r\n--{boundary}--".encode()
        status, resp = api(
            "POST",
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=multipart&fields=id",
            token, payload,
            f"multipart/related; boundary={boundary}",
        )
    else:
        status, resp = api(
            "POST",
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=media&fields=id",
            token, body, "application/json",
        )
    if status >= 300:
        sys.exit(f"upload failed: {status} {resp.decode()[:300]}")
    file_id = json.loads(resp)["id"]

    # anyone-with-link read permission
    status, resp = api(
        "POST",
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        token, json.dumps({"role": "reader", "type": "anyone"}).encode(),
        "application/json",
    )
    if status >= 300:
        api("DELETE", f"https://www.googleapis.com/drive/v3/files/{file_id}", token)
        sys.exit(f"permission failed: {status} {resp.decode()[:300]}")

    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    out = Path(__file__).parent / "creds_url.txt"
    out.write_text(url + "\n", encoding="utf-8")
    print(f"handoff file {file_id} created (public until the codespace deletes it)")
    print(f"URL written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
