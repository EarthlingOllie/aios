"""A fake Google Drive that replays recorded responses (`recordings/google_drive.json`): the test
seam for the Google Drive provider (`aios.source_providers.google_drive`). It's an httpx transport,
handed to the provider in place of the network, so nothing leaves the machine.

It starts as the recording does: an admin's My Drive (Policies, with HR under it; Private notes;
Archive) and the Engineering shared drive (Runbooks), holding Docs, Sheets, Slides, a PDF, an
image, an oversized scan, a Form and a Markdown file. A test changes it as Drive would change:
edit a file (`edit`), add one (`put`), move one (`move`), trash it (`trash`, its folder's contents
with it), delete it for good (`delete`), take it away from the admin (`unshare`), or change it
without the change feed saying (`delete(..., silently=True)`), or make a
native file too large to export (`too_large`). It can expire the access tokens it issued
(`expire_tokens`) or revoke the grant (`revoke`).

It checks what Google checks: the client id and secret, the authorisation code it issued, the
refresh token, and that every Drive call carries an access token it issued and hasn't expired. It
notes what it was asked (`requests`), what it exported and as what (`exports`), downloads
(`downloads`), access tokens minted (`minted`) and refresh tokens revoked (`revoked`).
"""

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

import httpx

RECORDED = json.loads((Path(__file__).parent / "recordings" / "google_drive.json").read_text())

CLIENT_ID = "recorded-client.apps.googleusercontent.com"
CLIENT_SECRET = "recorded-client-secret"
CODE = "4/recorded-authorisation-code"
REFRESH_TOKEN = RECORDED["token"]["refresh_token"]
FOLDER = "application/vnd.google-apps.folder"

EXPORTS = {
    "application/vnd.google-apps.document": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/pdf",
        "text/plain",
    },
    "application/vnd.google-apps.spreadsheet": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    },
    "application/vnd.google-apps.presentation": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/pdf",
    },
}

FILE_FIELDS = (
    "id",
    "name",
    "mimeType",
    "parents",
    "md5Checksum",
    "size",
    "modifiedTime",
    "trashed",
    "driveId",
    "shared",
    "version",
    "lastModifyingUser",
)


@dataclass
class FakeDrive:
    files: dict[str, dict] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    drives: list[dict] = field(default_factory=list)
    # Files the admin can no longer see (unshared): 404, and "removed" in the change feed.
    hidden: set[str] = field(default_factory=set)
    # The change log: each change's file id, in order; a page token is a position in it.
    log: list[str] = field(default_factory=list)
    page_size: int = 100
    requests: list[tuple[str, str]] = field(default_factory=list)
    exports: list[tuple[str, str]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    minted: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    expired: set[str] = field(default_factory=set)
    # Native files too large for Google to export (403).
    too_large: set[str] = field(default_factory=set)
    # The refresh tokens it honours.
    grants: set[str] = field(default_factory=set)
    _clock: int = 0

    def __post_init__(self) -> None:
        for row in RECORDED["files"]:
            row = copy.deepcopy(row)
            content = row.pop("content", None)
            row.setdefault("trashed", False)
            row["webViewLink"] = f"https://drive.google.com/open?id={row['id']}"
            self.files[row["id"]] = row
            if content is not None:
                self.contents[row["id"]] = content.encode()
        self.drives = copy.deepcopy(RECORDED["drives"])

    # --- Changing it, as Drive would change ------------------------------------------------------

    def _changed(self, file_id: str) -> None:
        self.log.append(file_id)

    def _touch(self, row: dict) -> None:
        self._clock += 1
        row["modifiedTime"] = f"2026-11-01T10:{self._clock:02d}:00.000Z"
        row["version"] = str(int(row.get("version", "1")) + 1)

    def edit(self, file_id: str, content: str) -> None:
        """Edit a file's content: a native file's export changes, a binary's checksum."""
        row = self.files[file_id]
        self.contents[file_id] = content.encode()
        if "md5Checksum" in row:
            row["md5Checksum"] = f"{abs(hash(content)):032x}"[:32]
            row["size"] = str(len(content.encode()))
        self._touch(row)
        self._changed(file_id)

    def put(self, file_id: str, name: str, mime_type: str, parent: str, content: str = "", **extra):
        """Add a file (or folder) under `parent`."""
        row = {
            "id": file_id,
            "name": name,
            "mimeType": mime_type,
            "parents": [parent],
            "trashed": False,
            "shared": True,
            "version": "1",
            "modifiedTime": "",
            "webViewLink": f"https://drive.google.com/open?id={file_id}",
            **extra,
        }
        if not mime_type.startswith("application/vnd.google-apps."):
            row.setdefault("md5Checksum", f"{abs(hash(content)):032x}"[:32])
            row.setdefault("size", str(len(content.encode())))
        drive = self.files.get(parent, {}).get("driveId")
        if drive:
            row["driveId"] = drive
        self.files[file_id] = row
        if mime_type != FOLDER:
            self.contents[file_id] = content.encode()
        self._touch(row)
        self._changed(file_id)

    def move(self, file_id: str, parent: str) -> None:
        row = self.files[file_id]
        row["parents"] = [parent]
        self._touch(row)
        self._changed(file_id)

    def _descendants(self, folder_id: str) -> list[str]:
        found = []
        for row in self.files.values():
            if folder_id in row.get("parents", []):
                found.append(row["id"])
                if row["mimeType"] == FOLDER:
                    found += self._descendants(row["id"])
        return found

    def trash(self, file_id: str) -> None:
        """Trash a file, or a folder and (without a change of their own, as Drive may) everything
        under it."""
        self.files[file_id]["trashed"] = True
        for child in self._descendants(file_id):
            self.files[child]["trashed"] = True
        self._changed(file_id)

    def delete(self, file_id: str, *, silently: bool = False) -> None:
        """Delete a file for good; `silently`, the change feed doesn't say (only a listing
        shows it)."""
        self.files.pop(file_id, None)
        self.contents.pop(file_id, None)
        if not silently:
            self._changed(file_id)

    def unshare(self, file_id: str) -> None:
        """Take a file away from the admin: it's no longer visible to aios."""
        self.hidden.add(file_id)
        self._changed(file_id)

    def expire_tokens(self) -> None:
        self.expired.update(self.minted)

    def revoke(self) -> None:
        """Revoke the grant at Google (as the admin removing aios's access would)."""
        self.grants.clear()

    # --- Serving it -------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, str(url)))
        if url.host == "oauth2.googleapis.com":
            if url.path == "/token":
                return self._token(request)
            if url.path == "/revoke":
                token = parse_qs(request.content.decode()).get("token", [""])[0]
                self.revoked.append(token)
                self.grants.discard(token)
                return httpx.Response(200, json={})
        if url.host != "www.googleapis.com":
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not found"}})
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ")
        if token not in self.minted or token in self.expired:
            return _error(401, "Request had invalid authentication credentials.")
        path = url.path.removeprefix("/drive/v3")
        params = dict(url.params)
        if path == "/about":
            return httpx.Response(200, json=RECORDED["about"])
        if path == "/drives":
            return httpx.Response(200, json={"drives": self.drives})
        if path == "/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.log))})
        if path == "/changes":
            return self._changes(params)
        if path == "/files":
            return self._list(params)
        if m := re.fullmatch(r"/files/([^/]+)/export", path):
            return self._export(m.group(1), params)
        if m := re.fullmatch(r"/files/([^/]+)", path):
            return self._get(m.group(1), params)
        return _error(404, "Not found")

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if (form.get("client_id"), form.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != CODE or not form.get("redirect_uri"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.grants.add(REFRESH_TOKEN)
            return httpx.Response(200, json={**RECORDED["token"], "access_token": self._mint()})
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in self.grants:
                return httpx.Response(
                    400,
                    json={"error": "invalid_grant", "error_description": "Token has been revoked."},
                )
            body = {k: v for k, v in RECORDED["token"].items() if k != "refresh_token"}
            return httpx.Response(200, json={**body, "access_token": self._mint()})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _mint(self) -> str:
        token = f"ya29.fake-{len(self.minted) + 1}"
        self.minted.append(token)
        return token

    def _visible(self, file_id: str) -> dict | None:
        if file_id in self.hidden:
            return None
        return self.files.get(file_id)

    def _get(self, file_id: str, params: dict) -> httpx.Response:
        row = self._visible(file_id)
        if row is None:
            return _error(404, f"File not found: {file_id}.")
        if params.get("alt") == "media":
            if row["mimeType"].startswith("application/vnd.google-apps."):
                return _error(403, "Only files with binary content can be downloaded.")
            self.downloads.append(file_id)
            return httpx.Response(200, content=self.contents.get(file_id, b""))
        return httpx.Response(200, json=_meta(row))

    def _export(self, file_id: str, params: dict) -> httpx.Response:
        row = self._visible(file_id)
        if row is None:
            return _error(404, f"File not found: {file_id}.")
        mime = params.get("mimeType", "")
        if file_id in self.too_large:
            return _error(403, "This file is too large to be exported.")
        if mime not in EXPORTS.get(row["mimeType"], set()):
            return _error(400, "Export only supports Docs Editors files.")
        self.exports.append((file_id, mime))
        return httpx.Response(200, content=self.contents.get(file_id, b""))

    def _list(self, params: dict) -> httpx.Response:
        q = params.get("q", "")
        if m := re.fullmatch(r"'([^']+)' in parents and trashed = false", q):
            rows = [
                r
                for r in self.files.values()
                if m.group(1) in r.get("parents", []) and not r["trashed"]
            ]
        elif q == f"mimeType = '{FOLDER}' and trashed = false":
            rows = [r for r in self.files.values() if r["mimeType"] == FOLDER and not r["trashed"]]
        else:
            return _error(400, f"Invalid query: {q}")
        rows = [r for r in rows if r["id"] not in self.hidden and r.get("parents")]
        rows.sort(key=lambda r: r["id"])
        size = min(int(params.get("pageSize", 100)), self.page_size)
        start = int(params.get("pageToken") or 0)
        page = rows[start : start + size]
        body: dict = {"files": [_meta(r) for r in page]}
        if start + size < len(rows):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)

    def _changes(self, params: dict) -> httpx.Response:
        start = int(params["pageToken"])
        size = min(int(params.get("pageSize", 100)), self.page_size)
        ids = self.log[start : start + size]
        changes = []
        for file_id in ids:
            row = self._visible(file_id)
            if row is None:
                changes.append({"changeType": "file", "fileId": file_id, "removed": True})
            else:
                changes.append(
                    {"changeType": "file", "fileId": file_id, "removed": False, "file": _meta(row)}
                )
        body: dict = {"changes": changes}
        if start + size < len(self.log):
            body["nextPageToken"] = str(start + size)
        else:
            body["newStartPageToken"] = str(len(self.log))
        return httpx.Response(200, json=body)


def _meta(row: dict) -> dict:
    return {k: copy.deepcopy(row[k]) for k in (*FILE_FIELDS, "webViewLink") if k in row}


def _error(code: int, message: str) -> httpx.Response:
    return httpx.Response(code, json={"error": {"code": code, "message": message}})
