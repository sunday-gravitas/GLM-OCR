#!/usr/bin/env python3
"""Delete the short-lived Drive handoff file after its contents were read.

Called by .devcontainer/fetch-drive-creds.sh right after the credentials
have been stored locally, so the public link stops working as soon as the
codespace has consumed it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse


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


def file_id_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    if "id" in qs:
        return qs["id"][0]
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", url)
    if m:
        return m.group(1)
    raise ValueError(f"cannot extract file id from {url!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", required=True)
    ap.add_argument("--url-file", required=True)
    args = ap.parse_args()

    creds = json.loads(Path(args.creds).read_text())
    url = Path(args.url_file).read_text().strip().splitlines()[0]
    file_id = file_id_from_url(url)

    token = refresh(creds)
    req = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 or exc.code == 204:
            return 0
        print(f"delete failed: {exc.code} {exc.read().decode()[:200]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
