"""Recorded HTTP exchanges, replayed: the provider tests' stand-in for the provider's API, so
nothing is sent. A recording (a JSON file under `recordings/`) is a list of exchanges, each a
request to match (method, path, and the query or form fields it must carry) and the response to
give (status, headers, JSON body). An exchange answers once, in order, unless it's marked
`repeat`. A request no exchange matches is answered 599 and noted (`unmatched`), so a test sees
it; the requests made are noted too (`requests`)."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

RECORDINGS = Path(__file__).parent / "recordings"


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((RECORDINGS / name).read_text())


class Replay:
    def __init__(self, *exchanges: dict[str, Any]) -> None:
        self.exchanges = [dict(e) for e in exchanges]
        self.used: set[int] = set()
        self.requests: list[httpx.Request] = []
        self.unmatched: list[str] = []

    def add(self, *exchanges: dict[str, Any]) -> "Replay":
        self.exchanges += [dict(e) for e in exchanges]
        return self

    @staticmethod
    def fields(request: httpx.Request) -> dict[str, str]:
        found = dict(request.url.params)
        body = request.content.decode() if request.content else ""
        kind = request.headers.get("content-type", "")
        if "json" in kind and body:
            found |= {k: str(v) for k, v in json.loads(body).items()}
        elif "form" in kind and body:
            found |= dict(parse_qsl(body))
        return found

    def calls(self, path: str) -> list[dict[str, str]]:
        """The fields of each request made to `path`."""
        return [self.fields(r) for r in self.requests if r.url.path == path]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fields = self.fields(request)
        for i, e in enumerate(self.exchanges):
            if i in self.used and not e.get("repeat"):
                continue
            if e.get("method", "GET") != request.method or e["path"] != request.url.path:
                continue
            want = e.get("query") or {}
            if any(fields.get(k) != str(v) for k, v in want.items()):
                continue
            if not e.get("repeat"):
                self.used.add(i)
            return httpx.Response(
                e.get("status", 200), headers=e.get("headers") or {}, json=e.get("json", {})
            )
        self.unmatched.append(f"{request.method} {request.url.path} {fields}")
        return httpx.Response(599, json={"ok": False, "error": "unrecorded"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)
