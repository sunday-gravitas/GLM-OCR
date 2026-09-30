"""Minimal Google Drive v3 REST client for the GLM-OCR book pipeline.

Authenticates with a refresh-token OAuth flow (no google-api-python-client
dependency). Only the handful of operations the pipeline needs are
implemented:

    - list children of a folder (paginated)
    - find / ensure subfolders (case-insensitive match)
    - download a file to a local path
    - upload a file (create or update-by-name) into a folder
    - read / write a JSON file living in Drive

Credentials are provided as a dict with at least:
    client_id, client_secret, refresh_token [, token_uri]
"""

from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 8


class DriveError(RuntimeError):
    """Raised when a Drive API call ultimately fails."""


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
        resp = requests.post(
            self.token_uri,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise DriveError(
                f"Drive token refresh failed ({resp.status_code}): {resp.text[:300]}"
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600)) - 120

    def _token(self) -> str:
        if not self._access_token or time.time() >= self._token_expiry:
            self._refresh_token()
        return self._access_token  # type: ignore[return-value]

    # ------------------------------------------------------------------ core
    def _request(
        self,
        method: str,
        url: str,
        timeout: int = 300,
        auth_retries: int = 2,
        **kwargs: Any,
    ) -> requests.Response:
        """Perform an authenticated request with retry/backoff semantics."""
        attempts = 0
        while attempts < MAX_ATTEMPTS:
            attempts += 1
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self._token()}"
            try:
                resp = requests.request(
                    method, url, headers=headers, timeout=timeout, **kwargs
                )
            except requests.RequestException as exc:
                if attempts >= MAX_ATTEMPTS:
                    raise DriveError(f"{method} {url}: {exc}") from exc
                time.sleep(min(2 ** attempts, 30))
                continue

            if resp.status_code == 401 and auth_retries > 0:
                # Access token probably expired mid-flight; force refresh once.
                auth_retries -= 1
                self._token_expiry = 0.0
                continue
            if resp.status_code in RETRY_STATUS and attempts < MAX_ATTEMPTS:
                time.sleep(min(2 ** attempts, 30))
                continue
            return resp

        raise DriveError(f"{method} {url}: exhausted retries")

    @staticmethod
    def _check(resp: requests.Response, what: str) -> requests.Response:
        if resp.status_code >= 400:
            raise DriveError(f"{what} failed ({resp.status_code}): {resp.text[:300]}")
        return resp

    # --------------------------------------------------------------- queries
    def list_children(self, folder_id: str) -> List[Dict[str, Any]]:
        """List non-trashed children of a folder. Returns id/name/mimeType/size."""
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
            resp = self._check(
                self._request("GET", f"{DRIVE_API}/files", params=params),
                "list_children",
            )
            data = resp.json()
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
        """Find a child folder by (case-insensitive) name; optionally create it."""
        for child in self.list_children(parent_id):
            if (
                child.get("mimeType") == "application/vnd.google-apps.folder"
                and self._norm(child["name"]) == self._norm(name)
            ):
                return child
        if not create:
            raise DriveError(f"Folder '{name}' not found under {parent_id}")
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        resp = self._check(
            self._request(
                "POST",
                f"{DRIVE_API}/files",
                params={"fields": "id, name, mimeType, parents"},
                json=body,
            ),
            f"create folder '{name}'",
        )
        return resp.json()

    def find_file(self, folder_id: str, name: str) -> Optional[Dict[str, Any]]:
        """Exact-name (case-sensitive) non-folder file lookup inside a folder."""
        for child in self.list_children(folder_id):
            if (
                child.get("mimeType") != "application/vnd.google-apps.folder"
                and child["name"] == name
            ):
                return child
        return None

    # ----------------------------------------------------------- transfer io
    def download_file(self, file_id: str, dest_path: Path) -> Path:
        resp = self._request(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={"alt": "media"},
            timeout=600,
        )
        self._check(resp, f"download {file_id}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        return dest_path

    def _upload_multipart(
        self, path: Path, parent_id: str, name: str, mime: str
    ) -> Dict[str, Any]:
        boundary = "glmocr-pipe-7163"
        metadata = json.dumps({"name": name, "parents": [parent_id]})
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{metadata}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--".encode("utf-8")
        headers = {"Content-Type": f"multipart/related; boundary={boundary}"}
        resp = self._check(
            self._request(
                "POST",
                f"{UPLOAD_API}/files",
                params={"uploadType": "multipart", "fields": "id, name, size"},
                headers=headers,
                data=body,
                timeout=1200,
            ),
            f"upload {name}",
        )
        return resp.json()

    def _update_media(self, file_id: str, path: Path, mime: str) -> Dict[str, Any]:
        headers = {"Content-Type": mime}
        resp = self._check(
            self._request(
                "PATCH",
                f"{UPLOAD_API}/files/{file_id}",
                params={"uploadType": "media", "fields": "id, name, size"},
                headers=headers,
                data=path.read_bytes(),
                timeout=1200,
            ),
            f"update {file_id}",
        )
        return resp.json()

    def upload_file(
        self,
        path: Path,
        parent_id: str,
        name: Optional[str] = None,
        replace: bool = True,
    ) -> Dict[str, Any]:
        """Upload a local file into a Drive folder (update if name exists)."""
        name = name or path.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        existing = self.find_file(parent_id, name)
        if existing and replace:
            result = self._update_media(existing["id"], path, mime)
            return {**result, "replaced": True}
        return self._upload_multipart(path, parent_id, name, mime)

    # --------------------------------------------------------------- json io
    def download_json(self, file_id: str) -> Any:
        resp = self._request(
            "GET", f"{DRIVE_API}/files/{file_id}", params={"alt": "media"}
        )
        self._check(resp, f"download json {file_id}")
        return json.loads(resp.content.decode("utf-8"))

    def upload_json(self, folder_id: str, name: str, payload: Any) -> Dict[str, Any]:
        tmp = Path("/tmp") / f".drive_upload_{int(time.time() * 1000)}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        try:
            return self.upload_file(tmp, folder_id, name=name, replace=True)
        finally:
            tmp.unlink(missing_ok=True)
