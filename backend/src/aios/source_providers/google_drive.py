"""The Google Drive provider: an OAuth client with read-only Drive access (`google_client_id`,
`google_client_secret`), unavailable without them. Read-only Drive access is a restricted scope:
until Google has verified the client (`google_drive_verified`), a production-like deployment
(`cookie_secure`) lists Drive as unavailable, with the reason, and doesn't offer connecting.

**Connecting.** An admin is sent to Google's consent page asking for offline, read-only access,
and Google sends them back with a code, exchanged for a refresh token. **Only the refresh token is
kept** as the connector's credential; access tokens are minted from it when a sync needs one, kept
in this process's memory until shortly before they expire, and never stored. A refresh token
Google no longer honours (revoked, or the admin removed aios's access) makes the connector need
reconnecting. Removing the connector revokes the grant at Google.

**Scope.** Folders in My Drive and shared drives (a shared drive itself, or a folder in one),
recursively. A My Drive folder that isn't shared with anyone is restricted: picking it needs the
acknowledgement.

**What syncs**: every file under the chosen folders, as a file item (named by its Drive file id)
that becomes a document. Google Docs, Sheets and Slides are exported to Word, Excel and PowerPoint
formats; PDFs, Office files, Markdown, text, CSV and HTML are downloaded as they are. Anything
else (images, Forms, shortcuts), and files over `drive_file_max_bytes`, are skipped, and listed so
on the sync record. A file's hash is its checksum and modified time, so an edited file is
re-exported (or downloaded) and re-ingested.

**Incremental sync** reads Drive's change feed from the page token the last sync kept (shared
drives included), keeping changes for files that are, or were, inside a chosen folder; each file's
ancestry is looked up once per sync. A file moved in is added; one moved out, trashed, deleted or
no longer visible is removed. A folder's change can't say which files came or went with it, so a
folder moved into or out of scope, trashed or deleted makes the sync list the chosen folders
afresh instead (`Changes.complete`). **Reconciliation** lists the chosen folders.

There are no Drive tools. Every call goes through `aios.outbound` (Google's hosts only).
`TRANSPORT` replaces the network in tests (a fake Drive replaying recorded responses).
"""

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from aios import outbound
from aios.config import get_settings
from aios.datastores.models import SyncedItemKind
from aios.source_providers import (
    Changes,
    Content,
    Grant,
    Item,
    NeedsReconnecting,
    Provider,
    ScopeOption,
    SkipItem,
    Skipped,
    SourceView,
    needs_app_settings,
)

NAME = "google_drive"
TITLE = "Google Drive"
AUTHORISE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
REVOKE = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CALLBACK = "/api/connectors/callback"
PAGE = 100
# Mint an access token afresh this long before it expires.
TOKEN_MARGIN_SECONDS = 5 * 60

FOLDER = "application/vnd.google-apps.folder"
NATIVE = "application/vnd.google-apps."
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
# Google's own formats, exported as: (content type, extension).
EXPORTS = {
    "application/vnd.google-apps.document": (DOCX, ".docx"),
    "application/vnd.google-apps.spreadsheet": (XLSX, ".xlsx"),
    "application/vnd.google-apps.presentation": (PPTX, ".pptx"),
}
EXPORTED = {content_type for content_type, _ in EXPORTS.values()}
# Google's formats as people know them, for a skipped file's reason.
NATIVE_NAMES = {
    "form": "Forms",
    "drawing": "Drawings",
    "site": "Sites",
    "map": "My Maps",
    "jam": "Jamboard",
    "shortcut": "shortcut",
    "script": "Apps Script",
}
# What's downloaded as it is: what ingest reads.
DOWNLOADED = {
    "application/pdf",
    DOCX,
    XLSX,
    PPTX,
    "text/markdown",
    "text/x-markdown",
    "text/plain",
    "text/csv",
    "text/html",
}
FIELDS = (
    "id,name,mimeType,parents,md5Checksum,size,modifiedTime,trashed,driveId,shared,version,"
    "webViewLink,lastModifyingUser(displayName)"
)
ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}

# The network, unless a test hands Drive's calls to a fake.
TRANSPORT: httpx.BaseTransport | None = None


class DriveError(Exception):
    """Drive refused a call, or couldn't answer it."""


def _unavailable() -> str | None:
    s = get_settings()
    if why := needs_app_settings(TITLE, s.google_client_id, s.google_client_secret):
        return why
    if s.cookie_secure and not s.google_drive_verified:
        return (
            "Google Drive is unavailable until Google has verified this deployment's access to "
            "Drive: connecting would end at Google's consent error."
        )
    return None


def _client() -> httpx.Client:
    return outbound.client(NAME, transport=TRANSPORT, headers={"User-Agent": "aios"})


def _said(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.reason_phrase
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or resp.reason_phrase)
    return str(body.get("error_description") or error or resp.reason_phrase)


# --- Access tokens -------------------------------------------------------------------------------

_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def forget_tokens() -> None:
    """Forget the access tokens minted in this process (tests)."""
    with _tokens_lock:
        _tokens.clear()


def _key(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode()).hexdigest()


def _keep(refresh_token: str, body: dict) -> str:
    token = body["access_token"]
    expires = time.monotonic() + float(body.get("expires_in", 3600)) - TOKEN_MARGIN_SECONDS
    with _tokens_lock:
        _tokens[_key(refresh_token)] = (token, expires)
    return token


def _token_call(form: dict[str, str]) -> httpx.Response:
    s = get_settings()
    form = {
        "client_id": s.google_client_id,
        "client_secret": s.google_client_secret.get_secret_value(),
        **form,
    }
    with _client() as http:
        return http.post(TOKEN, data=form)


def _access_token(refresh_token: str) -> str:
    with _tokens_lock:
        kept = _tokens.get(_key(refresh_token))
    if kept is not None and kept[1] > time.monotonic():
        return kept[0]
    resp = _token_call({"grant_type": "refresh_token", "refresh_token": refresh_token})
    if resp.status_code == 400 and _error_code(resp) == "invalid_grant":
        raise NeedsReconnecting("Google no longer honours aios's access to Drive.")
    if resp.status_code >= 400:
        raise DriveError(f"Google answered {resp.status_code} minting an access token.")
    return _keep(refresh_token, resp.json())


def _error_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error")
    except ValueError:
        return None


def _drop_token(refresh_token: str) -> None:
    with _tokens_lock:
        _tokens.pop(_key(refresh_token), None)


# --- Drive's API ---------------------------------------------------------------------------------


class _Drive:
    """Calls to Drive's API with a connector's credential, through one client: an expired access
    token is minted afresh once; a second refusal needs reconnecting."""

    def __init__(self, refresh_token: str | None):
        if not refresh_token:
            raise NeedsReconnecting("The Google Drive connector has no credentials.")
        self.refresh_token = refresh_token
        self.http = _client()

    def __enter__(self) -> "_Drive":
        return self

    def __exit__(self, *exc) -> None:
        self.http.close()

    def call(
        self, path: str, what: str, ok: tuple[int, ...] = (404,), **params: str
    ) -> httpx.Response:
        """The response to a GET of `path`: raises `DriveError` for a refusal not in `ok`."""
        for attempt in range(2):
            token = _access_token(self.refresh_token)
            resp = self.http.get(
                f"{API}{path}", params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code != 401:
                break
            _drop_token(self.refresh_token)
            if attempt:
                raise NeedsReconnecting("Google Drive refused aios's access.")
        if resp.status_code >= 400 and resp.status_code not in ok:
            raise DriveError(f"Google Drive answered {resp.status_code} {what}: {_said(resp)}")
        return resp

    def json(self, path: str, what: str, **params: str) -> dict:
        resp = self.call(path, what, **params)
        if resp.status_code == 404:
            raise DriveError(f"Google Drive found nothing {what}.")
        return resp.json()

    def file(self, file_id: str) -> dict | None:
        """A file's metadata; None if it's gone, or no longer visible."""
        resp = self.call(
            f"/files/{file_id}", f"reading file {file_id}", fields=FIELDS, supportsAllDrives="true"
        )
        return None if resp.status_code == 404 else resp.json()

    def files(self, q: str, what: str) -> Iterator[dict]:
        token = None
        while True:
            params = {"q": q, "fields": f"nextPageToken,files({FIELDS})", "pageSize": str(PAGE)}
            params |= {"corpora": "allDrives", **ALL_DRIVES}
            if token:
                params["pageToken"] = token
            page = self.json("/files", what, **params)
            yield from page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return

    def start_token(self) -> str:
        return self.json(
            "/changes/startPageToken", "starting the change feed", supportsAllDrives="true"
        )["startPageToken"]

    def changes(self, token: str) -> tuple[list[dict], str]:
        """Every change since the page token, and the page token to resume from."""
        found: list[dict] = []
        while True:
            page = self.json(
                "/changes",
                "reading the change feed",
                pageToken=token,
                pageSize=str(PAGE),
                includeRemoved="true",
                fields=f"nextPageToken,newStartPageToken,changes(changeType,removed,fileId,"
                f"file({FIELDS}))",
                **ALL_DRIVES,
            )
            found += page.get("changes", [])
            if page.get("newStartPageToken"):
                return found, page["newStartPageToken"]
            token = page["nextPageToken"]


# --- Files as items ------------------------------------------------------------------------------


def _hash(f: dict) -> str:
    said = f"{f.get('md5Checksum', '')}|{f.get('modifiedTime', '')}|{f.get('version', '')}"
    return hashlib.sha256(said.encode()).hexdigest()


def _when(f: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def _mib(n: int) -> str:
    return f"{n / (1024 * 1024):g} MiB"


def classify(f: dict) -> Item | Skipped:
    """A Drive file as an item to sync, or as skipped (and why)."""
    mime = f.get("mimeType", "")
    name = f.get("name") or f["id"]
    item = {
        "external_id": f["id"],
        "kind": SyncedItemKind.file,
        "content_hash": _hash(f),
        "url": f.get("webViewLink"),
        "author_name": (f.get("lastModifyingUser") or {}).get("displayName"),
        "updated_at": _when(f),
    }
    if mime in EXPORTS:
        content_type, extension = EXPORTS[mime]
        return Item(title=name + extension, content_type=content_type, **item)
    if mime.startswith(NATIVE):
        kind = mime.removeprefix(NATIVE)
        label = NATIVE_NAMES.get(kind, kind)
        return Skipped(f["id"], name, f"Google {label} files can't be exported.")
    if mime not in DOWNLOADED:
        return Skipped(f["id"], name, f"Unsupported file type ({mime or 'unknown'}).")
    cap = get_settings().drive_file_max_bytes
    if int(f.get("size") or 0) > cap:
        return Skipped(f["id"], name, f"Larger than the {_mib(cap)} file cap.")
    return Item(title=name, content_type=mime, **item)


class _Tree:
    """Which files are inside the chosen folders, from their ancestry, each folder looked up at
    most once."""

    def __init__(self, drive: _Drive, scope: set[str]):
        self.drive = drive
        self.scope = scope
        self.inside: dict[str, bool] = {}

    def contains(self, f: dict) -> bool:
        """Whether the file (or folder) is under a chosen folder (a chosen folder is under
        itself)."""
        return self._in(f, set())

    def _in(self, f: dict, seen: set[str]) -> bool:
        if f["id"] in self.scope:
            return True
        if f.get("trashed"):
            return False
        for parent in f.get("parents") or []:
            if parent in self.scope or self._folder_in(parent, seen):
                return True
        return False

    def _folder_in(self, folder_id: str, seen: set[str]) -> bool:
        if folder_id in self.inside:
            return self.inside[folder_id]
        if folder_id in seen:
            return False
        seen.add(folder_id)
        meta = self.drive.file(folder_id)
        self.inside[folder_id] = meta is not None and self._in(meta, seen)
        return self.inside[folder_id]


def _scope(source: SourceView) -> list[str]:
    return list(source.settings.get("scope") or [])


class DriveAdapter:
    """The adapter (`aios.source_providers.Adapter`) over a connector's chosen Drive folders."""

    def scope_options(self, source: SourceView) -> list[ScopeOption]:
        with _Drive(source.credential) as drive:
            drives = drive.json("/drives", "listing shared drives", pageSize="100").get(
                "drives", []
            )
            folders = list(drive.files(f"mimeType = '{FOLDER}' and trashed = false", "listing"))
        by_id = {f["id"]: f for f in folders}
        names = {d["id"]: d["name"] for d in drives}

        def path(f: dict, depth: int = 0) -> str:
            parents = [p for p in f.get("parents") or [] if p in by_id or p in names]
            if not parents or depth > 20:
                return f["name"]
            parent = parents[0]
            above = names[parent] if parent in names else path(by_id[parent], depth + 1)
            return f"{above} / {f['name']}"

        options = [ScopeOption(d["id"], f"{d['name']} (shared drive)") for d in drives]
        options += [
            ScopeOption(f["id"], path(f), restricted=not f.get("driveId") and not f.get("shared"))
            for f in folders
        ]
        return options

    def listing(self, source: SourceView) -> Changes:
        with _Drive(source.credential) as drive:
            token = drive.start_token()
            return self._listed(drive, source, token)

    def _listed(self, drive: _Drive, source: SourceView, token: str) -> Changes:
        """Everything under the chosen folders, with the cursor to read changes from next."""
        changed: dict[str, Item] = {}
        skipped: dict[str, Skipped] = {}
        known: set[str] = set()
        queue = []
        for root in _scope(source):
            meta = drive.file(root)
            if meta is not None and not meta.get("trashed"):
                queue.append(root)
        while queue:
            folder = queue.pop(0)
            if folder in known:
                continue
            known.add(folder)
            for f in drive.files(f"'{folder}' in parents and trashed = false", "listing a folder"):
                if f.get("mimeType") == FOLDER:
                    queue.append(f["id"])
                    continue
                found = classify(f)
                if isinstance(found, Item):
                    changed[found.external_id] = found
                else:
                    skipped[found.external_id] = found
        return Changes(
            list(changed.values()),
            [],
            _cursor(token, known),
            list(skipped.values()),
            complete=True,
        )

    def changes(self, source: SourceView, cursor: str | None) -> Changes:
        if not cursor:
            return self.listing(source)
        at = json.loads(cursor)
        known = set(at.get("folders") or [])
        with _Drive(source.credential) as drive:
            feed, token = drive.changes(at["token"])
            tree = _Tree(drive, set(_scope(source)))
            changed: dict[str, Item] = {}
            skipped: dict[str, Skipped] = {}
            removed: set[str] = set()
            relist = False
            for change in feed:
                if change.get("changeType", "file") != "file":
                    continue
                file_id = change["fileId"]
                f = change.get("file")
                gone = bool(change.get("removed")) or f is None or bool(f.get("trashed"))
                if file_id in known or (f is not None and f.get("mimeType") == FOLDER):
                    # A folder: whether it's in scope now against whether it was.
                    if (file_id in known) != (not gone and tree.contains(f)):
                        relist = True
                    continue
                changed.pop(file_id, None)
                skipped.pop(file_id, None)
                if gone or not tree.contains(f):
                    removed.add(file_id)
                    continue
                removed.discard(file_id)
                found = classify(f)
                if isinstance(found, Item):
                    changed[file_id] = found
                else:
                    skipped[file_id] = found
            if relist:
                return self._listed(drive, source, token)
        return Changes(
            list(changed.values()), sorted(removed), _cursor(token, known), list(skipped.values())
        )

    def fetch(self, source: SourceView, item: Item) -> Content:
        cap = get_settings().drive_file_max_bytes
        with _Drive(source.credential) as drive:
            native = None
            if item.content_type in EXPORTED:
                # Exported, or an Office file downloaded as it is: Drive says which.
                meta = drive.file(item.external_id)
                if meta is None:
                    raise DriveError(f"{item.title} is no longer in Google Drive.")
                native = meta.get("mimeType", "").startswith(NATIVE) or None
            if native:
                resp = drive.call(
                    f"/files/{item.external_id}/export",
                    f"exporting {item.title}",
                    ok=(403, 404),
                    mimeType=item.content_type,
                )
                if resp.status_code == 403:
                    raise SkipItem(f"Google couldn't export it: {_said(resp)}")
            else:
                resp = drive.call(
                    f"/files/{item.external_id}",
                    f"downloading {item.title}",
                    alt="media",
                    supportsAllDrives="true",
                )
            if resp.status_code == 404:
                raise DriveError(f"{item.title} is no longer in Google Drive.")
        if len(resp.content) > cap:
            raise SkipItem(f"Larger than the {_mib(cap)} file cap.")
        return Content(data=resp.content)


def _cursor(token: str, folders: set[str]) -> str:
    return json.dumps({"token": token, "folders": sorted(folders)}, sort_keys=True)


# --- Connecting ----------------------------------------------------------------------------------


def _redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + CALLBACK


def _authorise_url(state: str) -> str:
    query = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTHORISE}?{urlencode(query)}"


def _exchange(code: str, **_: Any) -> Grant:
    resp = _token_call(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri()}
    )
    if resp.status_code >= 400:
        raise DriveError(f"Google didn't accept the authorisation: {_said(resp)}")
    body = resp.json()
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise DriveError("Google didn't grant offline access.")
    _keep(refresh_token, body)
    with _Drive(refresh_token) as drive:
        about = drive.json("/about", "reading the account", fields="user(emailAddress)")
    return Grant(
        secret=refresh_token,
        scopes=body.get("scope"),
        external_account=(about.get("user") or {}).get("emailAddress"),
    )


def _revoke(refresh_token: str) -> None:
    _drop_token(refresh_token)
    with _client() as http:
        resp = http.post(REVOKE, data={"token": refresh_token})
    if resp.status_code >= 400 and _error_code(resp) != "invalid_token":
        raise DriveError(f"Google answered {resp.status_code} revoking access.")


PROVIDER = Provider(
    NAME,
    TITLE,
    _unavailable,
    adapter=DriveAdapter,
    authorise_url=_authorise_url,
    exchange=_exchange,
    revoke=_revoke,
)
