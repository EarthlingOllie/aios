"""URL sources: a web page fetched on its source's interval (`aios.sync`), through the
request-forgery guard (`aios.url_fetch`).

The page is one file item, named by its URL, that becomes a document. Each fetch is conditional
on the page's last ETag and Last-Modified (kept in the source's cursor with the page's content
hash): "not modified", or the same content, isn't fetched or ingested again; changed content
replaces the document's. "Not found" and "gone" remove the item, and its document, while the
source stays active and keeps fetching. An address that isn't reachable fails the sync.
"""

import hashlib
import json
import re
from typing import Any

import httpx

from aios import url_fetch
from aios.datastores.models import SyncedItemKind
from aios.source_providers import Changes, Content, Item, ScopeOption, SourceView

EXTENSIONS = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/pdf": ".pdf",
    "application/json": ".json",
}


def filename(url: str, content_type: str | None) -> str:
    """The page's document's name: its host and path, with an extension for its type."""
    parsed = httpx.URL(url)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{parsed.host}{parsed.path}").strip("-.") or "page"
    extension = EXTENSIONS.get(content_type or "", "")
    if extension and not stem.lower().endswith(extension):
        stem += extension
    return stem[:255]


def _cursor(raw: str | None) -> dict[str, Any]:
    try:
        found = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


class UrlAdapter:
    def __init__(self) -> None:
        # The bodies fetched this sync, by URL, which `fetch` gives the loop.
        self._bodies: dict[str, bytes] = {}

    def _page(self, source: SourceView, cursor: dict[str, Any]) -> Changes:
        url = source.settings["url"]
        fetched = url_fetch.fetch(
            url, etag=cursor.get("etag"), last_modified=cursor.get("last_modified")
        )
        if fetched.status == url_fetch.GONE:
            return Changes(removed=[url], cursor=None)
        if fetched.status == url_fetch.NOT_MODIFIED and cursor.get("hash"):
            item = Item(
                external_id=url,
                kind=SyncedItemKind.file,
                title=cursor["title"],
                content_hash=cursor["hash"],
                url=cursor.get("url", url),
                content_type=cursor.get("content_type"),
            )
            return Changes(changed=[item], cursor=json.dumps(cursor))
        if fetched.status == url_fetch.NOT_MODIFIED:
            # Nothing to compare with: fetch it whole.
            return self._page(source, {})
        hashed = hashlib.sha256(fetched.body).hexdigest()
        title = filename(fetched.url, fetched.content_type)
        self._bodies[url] = fetched.body
        item = Item(
            external_id=url,
            kind=SyncedItemKind.file,
            title=title,
            content_hash=hashed,
            url=fetched.url,
            content_type=fetched.content_type,
        )
        kept = {
            "etag": fetched.etag,
            "last_modified": fetched.last_modified,
            "hash": hashed,
            "title": title,
            "url": fetched.url,
            "content_type": fetched.content_type,
        }
        return Changes(changed=[item], cursor=json.dumps(kept))

    def changes(self, source: SourceView, cursor: str | None) -> Changes:
        return self._page(source, _cursor(cursor))

    def listing(self, source: SourceView) -> Changes:
        return self._page(source, {})

    def fetch(self, source: SourceView, item: Item) -> Content:
        if item.external_id not in self._bodies:
            self._page(source, {})
        return Content(data=self._bodies[item.external_id])

    def scope_options(self, source: SourceView) -> list[ScopeOption]:
        return []
