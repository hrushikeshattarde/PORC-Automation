"""Minimal Transport Pro Public API client used by the PORC bot.

Endpoints (from the Transport Pro Postman collection / TPro MCP server):
  POST /auth                       Basic auth -> {access_token, refresh_token, expires_in}
  GET  /load/search                requires a date window (<= 60 days); zero-based ?page=
  GET  /load/{id}                  full load record
  GET  /load/{id}/notes            load + dispatch notes
  GET  /dispatch/search?loadId=    dispatches for a load
  GET  /files/search               recordType=loads&recordId=...&fileTypeId=...
  GET  /files/{id}                 binary download
  POST /files/upload               multipart: recordType, recordId, documentType, comments, file
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

# The TPro edge/WAF returns 403 for the default python-requests user agent.
USER_AGENT = "Mozilla/5.0 (compatible; PORC-Automation/0.1)"


class TProError(RuntimeError):
    def __init__(self, status: int, message: str, request: str):
        super().__init__(f"{request} -> HTTP {status}: {message}")
        self.status = status
        self.body = message
        self.request = request


class TProClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._token: str | None = None
        self._refresh: str | None = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------ auth
    def _login(self) -> None:
        r = self.session.post(f"{self.base_url}/auth", auth=(self.username, self.password), timeout=self.timeout)
        if not r.ok:
            raise TProError(r.status_code, r.text[:300], "POST /auth")
        data = r.json()
        self._token = data.get("access_token") or data.get("accessToken") or data.get("token")
        self._refresh = data.get("refresh_token") or data.get("refreshToken")
        ttl = float(data.get("expires_in") or 3600)
        self._expires_at = time.monotonic() + max(ttl - 60, 60)
        if not self._token:
            raise TProError(200, "auth response had no access token", "POST /auth")
        self.session.headers["Authorization"] = f"Bearer {self._token}"
        log.debug("TPro login ok (token ttl %ss)", ttl)

    def _ensure_token(self, force: bool = False) -> None:
        if force or not self._token or time.monotonic() >= self._expires_at:
            self._login()

    # --------------------------------------------------------------- request
    def _request(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None,
                 files: dict | None = None, stream: bool = False, _retry: bool = True) -> requests.Response:
        self._ensure_token()
        clean_params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{self.base_url}{path}"
        r = self.session.request(method, url, params=clean_params, data=data, files=files,
                                 timeout=self.timeout, stream=stream)
        if r.status_code == 401 and _retry:
            self._ensure_token(force=True)
            return self._request(method, path, params=params, data=data, files=files, stream=stream, _retry=False)
        if not r.ok:
            raise TProError(r.status_code, r.text[:500], f"{method} {path} {clean_params}")
        return r

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params).json()

    def iter_pages(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = 0
        while True:
            data = self.get(path, {**(params or {}), "page": page}) or {}
            for row in (data.get("results") or []):
                yield row
            pagination = data.get("pagination") or {}
            page += 1
            if page >= int(pagination.get("totalPages") or 1):
                break

    # ------------------------------------------------------------------ loads
    def search_delivered_loads(self, customer_id: int, delivery_start: str, delivery_end: str) -> list[dict]:
        """Delivered loads for a customer whose delivery date falls in [start, end] (YYYY-MM-DD, <= 60 days)."""
        return list(self.iter_pages("/load/search", {
            "customerId": customer_id,
            "loadStatus": "delivered",
            "deliveryDateStart": delivery_start,
            "deliveryDateEnd": delivery_end,
        }))

    def get_load(self, load_id: int) -> dict:
        return self.get(f"/load/{load_id}")

    def get_load_notes(self, load_id: int) -> list[dict]:
        return list(self.iter_pages(f"/load/{load_id}/notes"))

    def get_dispatches(self, load_id: int) -> list[dict]:
        try:
            return list(self.iter_pages("/dispatch/search", {"loadId": load_id}))
        except TProError as exc:
            log.warning("dispatch search failed for load %s: %s", load_id, exc)
            return []

    # ------------------------------------------------------------------ files
    def list_files(self, load_id: int, file_type_id: int | None = None) -> list[dict]:
        return list(self.iter_pages("/files/search", {
            "recordType": "loads", "recordId": load_id, "fileTypeId": file_type_id,
        }))

    def download_file(self, file_id: int, dest: Path) -> Path:
        """GET /files/{id} answers JSON {id, fileName, mimeType, ..., fileData: <base64>} (not raw bytes).
        Decode it and refuse anything that is not a real PDF/image so a JSON blob can never be uploaded."""
        r = self._request("GET", f"/files/{file_id}")
        content_type = (r.headers.get("Content-Type") or "").lower()
        if "json" in content_type or r.content[:1] == b"{":
            payload = r.json()
            data = payload.get("fileData") or payload.get("data") or payload.get("content")
            if not data:
                raise TProError(200, f"file {file_id}: JSON response has no fileData", f"GET /files/{file_id}")
            content = base64.b64decode(data)
        else:
            content = r.content
        if not (content.startswith(b"%PDF") or content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"II*\x00", b"MM\x00*")):
            raise TProError(200, f"file {file_id}: decoded content is not a PDF/image (starts with {content[:8]!r})",
                            f"GET /files/{file_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return dest

    def upload_file(self, load_id: int, file_path: Path, document_type: str, comments: str = "") -> Any:
        with open(file_path, "rb") as fh:
            r = self._request(
                "POST", "/files/upload",
                data={"recordType": "Loads", "recordId": str(load_id),
                      "documentType": document_type, "comments": comments},
                files={"file": (file_path.name, fh, "application/pdf")},
            )
        try:
            return r.json()
        except ValueError:
            return {"status": r.status_code, "text": r.text[:300]}
