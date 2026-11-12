"""Reading server-sent events streams in tests: `open_stream` opens one through the app, as the
test client would (signed in as it is, under the organisation prefix as it puts a path), and
collects its events in the background until the test closes it; `until` waits for a condition
on what has arrived.

The test client answers once a response has ended, which a stream never does, so the stream is
driven here, through the app's ASGI interface, on an event loop of its own: requests the test
makes meanwhile (posting a message, say) go through the client as usual."""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from conftest import OrganisationClient, unscoped

from aios.main import app


@dataclass(frozen=True)
class Event:
    event: str
    data: Any
    id: int | None = None


def parse(text: str) -> tuple[list[Event], str]:
    """The complete events in `text`, and what's left of an incomplete one. Comments and
    `retry` lines aren't events."""
    events = []
    *blocks, rest = text.split("\n\n")
    for block in blocks:
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            fields[name] = fields.get(name, "") + value.removeprefix(" ")
        if "event" not in fields and "data" not in fields:
            continue
        events.append(
            Event(
                fields.get("event", "message"),
                json.loads(fields["data"]) if fields.get("data") else None,
                int(fields["id"]) if fields.get("id") else None,
            )
        )
    return events, rest


class Stream:
    """An open stream (`open_stream`): its status and headers once it has answered, the events
    it has sent so far (`events`), and its body if it wasn't a stream (`json`)."""

    def __init__(self, path: str, headers: list[tuple[bytes, bytes]]) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.events: list[Event] = []
        self.ended = False
        self._body = b""
        self._text = ""
        self._changed = threading.Condition()
        self._stop = threading.Event()
        split = urlsplit(path)
        self._scope = {
            "type": "http",
            # Before 2.4, so a disconnect ends the response at once, as a server's does.
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": split.path,
            "raw_path": split.path.encode(),
            "root_path": "",
            "query_string": split.query.encode(),
            "headers": [(b"host", b"testserver"), *headers],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        with self._changed:
            if not self._changed.wait_for(lambda: self.status is not None, timeout=10):
                raise TimeoutError(f"{path} didn't answer")

    def _run(self) -> None:
        asyncio.run(self._drive())

    async def _drive(self) -> None:
        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            while not self._stop.is_set():
                await asyncio.sleep(0.02)
            return {"type": "http.disconnect"}

        async def send(message):
            with self._changed:
                if message["type"] == "http.response.start":
                    self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
                    self.status = message["status"]
                elif message["type"] == "http.response.body":
                    self._body += message.get("body", b"")
                    if self.headers.get("content-type", "").startswith("text/event-stream"):
                        self._text += message.get("body", b"").decode()
                        events, self._text = parse(self._text)
                        self.events += events
                self._changed.notify_all()

        try:
            await app(self._scope, receive, send)
        finally:
            with self._changed:
                self.ended = True
                self._changed.notify_all()

    @property
    def json(self) -> Any:
        with self._changed:
            self._changed.wait_for(lambda: self.ended, timeout=10)
        return json.loads(self._body)

    def until(self, condition: Callable[[list[Event]], Any], timeout: float = 10) -> list[Event]:
        """The events so far, once `condition` holds of them (a `TimeoutError` if it doesn't
        within `timeout` seconds, or the stream ends without it)."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while not condition(self.events):
                left = deadline - time.monotonic()
                if left <= 0 or self.ended:
                    raise TimeoutError(f"not seen in {self.events}")
                self._changed.wait(left)
            return list(self.events)

    def of(self, event: str) -> list[Event]:
        with self._changed:
            return [e for e in self.events if e.event == event]

    def wait_ended(self, timeout: float = 10) -> None:
        with self._changed:
            if not self._changed.wait_for(lambda: self.ended, timeout=timeout):
                raise TimeoutError("the stream didn't end")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def __enter__(self) -> "Stream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_stream(client: OrganisationClient, path: str, last_event_id: int | None = None) -> Stream:
    """Open the stream at `path` as `client` would request it (see the module docstring)."""
    if path.startswith("/api/") and not unscoped(path):
        org_id = client._organisation_id()
        if org_id is not None:
            path = f"/api/o/{org_id}/{path.removeprefix('/api/')}"
    headers = [(b"accept", b"text/event-stream")]
    if client.cookies:
        cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        headers.append((b"cookie", cookie.encode()))
    if last_event_id is not None:
        headers.append((b"last-event-id", str(last_event_id).encode()))
    return Stream(path, headers)


def messages(events: list[Event]) -> list[Event]:
    return [e for e in events if e.event == "message"]
