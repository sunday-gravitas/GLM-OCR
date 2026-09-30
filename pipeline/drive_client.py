"""Minimal Google Drive v3 client for the GLM-OCR book pipeline.

Implemented on the Python standard library (urllib) only — deliberately
avoiding ``requests`` so that the Drive path keeps working even if pip
packages are damaged (e.g. by an interrupted install), which is exactly
what happened during the initial codespace bring-up.

Authenticates with a refresh-token OAuth flow. Operations:
    - list children of a folder (paginated)
    - find / ensure subfolders (case-insensitive match)
    - download a file to a local path
    - upload a file (create or update-by-name) into a folder
    - read / write a JSON file living in Drive
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6


class DriveError(RuntimeError):
    """Raised when a Drive API call ultimately fails."""


def _run_bounded(fn, seconds: float):
    """Run fn() with a hard wall-clock bound.

    Network calls can block forever when DNS/egress hangs (socket timeouts
    do NOT cover getaddrinfo). We run the call in a worker thread and simply
    ABANDON the thread if it exceeds the bound — the leaked thread is
    harmless, and the caller can retry with a fresh connection.
    """
    result: Dict[str, Any] = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            result["exc"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if "value" in result:
        return result["value"]
    if "exc" in result:
        raise result["exc"]
    raise TimeoutError(f"network call abandoned after {seconds}s (hang)")


class DriveClient:
    def __init__(self, creds: Dict[str, Any]):
        self.client_id = creds["client_id"]
        self.client_secret = creds["client_secret"]
        self.refresh_token = creds["refresh_token"]
        self.token_uri = creds.get("token_uri") or DEFAULT_TOKEN_URI
        self._access_token: Optional[str] = None
        self._token_expiry = 0.0

    # ------------------------------------------------------------------ auth
    def _refresh_token(self) -> None:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }).encode()
        try:
            def _do():
                req = urllib.request.Request(self.token_uri, data=data)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read())
            payload = _run_bounded(_do, seconds=90)
        except urllib.error.HTTPError as exc:
            raise DriveError(
                f"Drive token refresh failed ({exc.code}): "
                f"{exc.read().decode()[:300]}"
            ) from exc
        except OSError as exc:
            raise DriveError(f"Drive token refresh failed: {exc}") from exc
        self._access_token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600)) - 120

    def _token(self) -> str:
        if not self._access_token or time.time() >= self._token_expiry:
            self._refresh_token()
        return self._access_token  # type: ignore[return-value]

    # ------------------------------------------------------------------ core
    def _request(
        self,
        method: str,
        url: str,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        timeout: int = 75,
        auth_retries: int = 2,
    ) -> Dict[str, Any]:
        """Perform an authenticated request. Returns {'status', 'bytes'}."""
        attempts = 0
        last: Dict[str, Any] = {"status": 0, "bytes": b""}
        while attempts < MAX_ATTEMPTS:
            attempts += 1
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Authorization", f"Bearer {self._token()}")
            if content_type:
                req.add_header("Content-Type", content_type)
            try:
                def _do(r=req):
                    with urllib.request.urlopen(r, timeout=timeout) as resp:
                        return {"status": resp.status, "bytes": resp.read()}
                return _run_bounded(_do, seconds=timeout + 20)
            except urllib.error.HTTPError as exc:
                status = exc.code
                payload = exc.read()
                if status == 401 and auth_retries > 0:
                    auth_retries -= 1
                    self._token_expiry = 0.0
                    continue
                if status in RETRY_STATUS and attempts < MAX_ATTEMPTS:
                    time.sleep(min(2 ** attempts, 30))
                    continue
                last = {"status": status, "bytes": payload, "error": True}
                break
            except (OSError, TimeoutError) as exc:
                if attempts >= MAX_ATTEMPTS:
                    raise DriveError(f"{method} {url}: {exc}") from exc
                time.sleep(min(2 ** attempts, 30))
                continue
        return last

    @staticmethod
    def _check(result: Dict[str, Any], what: str) -> Dict[str, Any]:
        if result.get("error") or result["status"] >= 400:
            raise DriveError(
                f"{what} failed ({result['status']}): "
                f"{result['bytes'].decode(errors='replace')[:300]}"
            )
        return result

    # --------------------------------------------------------------- queries
    def list_children(self, folder_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                "pageSize": 200,
            }
            if page_token:
                params["pageToken"] = page_token
            url = f"{DRIVE_API}/files?{urllib.parse.urlencode(params)}"
            result = self._check(
                self._request("GET", url), "list_children"
            )
            data = json.loads(result["bytes"])
            out.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    @staticmethod
    def _norm(name: str) -> str:
        return name.strip().lower()

    def find_folder(
        self, parent_id: str, name: str, create: bool = True
    ) -> Dict[str, Any]:
        for child in self.list_children(parent_id):
            if (
                child.get("mimeType") == "application/vnd.google-apps.folder"
                and self._norm(child["name"]) == self._norm(name)
            ):
                return child
        if not create:
            raise DriveError(f"Folder '{name}' not found under {parent_id}")
        meta = json.dumps({
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }).encode()
        url = f"{DRIVE_API}/files?fields=id,name,mimeType,parents"
        result = self._check(
            self._request("POST", url, body=meta,
                          content_type="application/json"),
            f"create folder '{name}'",
        )
        return json.loads(result["bytes"])

    def find_file(self, folder_id: str, name: str) -> Optional[Dict[str, Any]]:
        for child in self.list_children(folder_id):
            if (
                child.get("mimeType") != "application/vnd.google-apps.folder"
                and child["name"] == name
            ):
                return child
        return None

    # ----------------------------------------------------------- transfer io
    def download_file(self, file_id: str, dest_path: Path) -> Path:
        url = f"{DRIVE_API}/files/{file_id}?alt=media"
        result = self._check(
            self._request("GET", url, timeout=600), f"download {file_id}"
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(result["bytes"])
        return dest_path

    def _upload_multipart(
        self, path: Path, parent_id: str, name: str, mime: str
    ) -> Dict[str, Any]:
        boundary = "glmocr-pipe-7163"
        meta = json.dumps({"name": name, "parents": [parent_id]})
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{meta}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--".encode()
        url = f"{UPLOAD_API}/files?uploadType=multipart&fields=id,name,size"
        result = self._check(
            self._request("POST", url, body=body, timeout=1200,
                          content_type=f"multipart/related; boundary={boundary}"),
            f"upload {name}",
        )
        return json.loads(result["bytes"])

    def _update_media(self, file_id: str, path: Path, mime: str) -> Dict[str, Any]:
        url = (f"{UPLOAD_API}/files/{file_id}"
               f"?uploadType=media&fields=id,name,size")
        result = self._check(
            self._request("PATCH", url, body=path.read_bytes(),
                          timeout=1200, content_type=mime),
            f"update {file_id}",
        )
        return json.loads(result["bytes"])

    def upload_file(
        self,
        path: Path,
        parent_id: str,
        name: Optional[str] = None,
        replace: bool = True,
    ) -> Dict[str, Any]:
        name = name or path.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        existing = self.find_file(parent_id, name)
        if existing and replace:
            result = self._update_media(existing["id"], path, mime)
            return {**result, "replaced": True}
        return self._upload_multipart(path, parent_id, name, mime)

    # --------------------------------------------------------------- json io
    def download_json(self, file_id: str) -> Any:
        url = f"{DRIVE_API}/files/{file_id}?alt=media"
        result = self._check(
            self._request("GET", url), f"download json {file_id}"
        )
        return json.loads(result["bytes"].decode("utf-8"))

    def upload_json(self, folder_id: str, name: str, payload: Any) -> Dict[str, Any]:
        tmp = Path("/tmp") / f".drive_upload_{int(time.time() * 1000)}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        try:
            return self.upload_file(tmp, folder_id, name=name, replace=True)
        finally:
            tmp.unlink(missing_ok=True)
