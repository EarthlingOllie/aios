"""Fetching a URL source's page (`aios.url_fetch`): the request-forgery suite, the limits, and
conditional requests. The network is a fake: names resolve through a fake resolver, and the
connection is a mock transport that records the address it was made to, so nothing leaves the
machine."""

import time

import httpx
import pytest

from aios import url_fetch
from aios.config import get_settings

PUBLIC = "93.184.216.34"
OTHER_PUBLIC = "93.184.216.35"


class Net:
    """Names and pages: `names` maps a host to the addresses it resolves to (a list per call
    for a name whose answer changes); `pages` maps (address, path) to a handler."""

    def __init__(self):
        self.names: dict[str, list] = {}
        self.pages: dict[tuple[str, str], object] = {}
        self.connected: list[str] = []
        self.requests: list[httpx.Request] = []

    def resolve(self, host: str) -> list[str]:
        answer = self.names.get(host, [])
        if answer and isinstance(answer[0], list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.connected.append(request.url.host)
        self.requests.append(request)
        page = self.pages.get((request.url.host, request.url.path))
        if page is None:
            return httpx.Response(404)
        return page(request) if callable(page) else page


@pytest.fixture
def net(monkeypatch) -> Net:
    net = Net()
    monkeypatch.setattr(url_fetch, "resolve", net.resolve)
    monkeypatch.setattr(url_fetch, "transport", lambda: httpx.MockTransport(net.handle))
    return net


def page(body: bytes = b"<p>Prices</p>", **headers) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "text/html", **headers})


def test_a_public_page_is_fetched_from_the_address_that_was_checked(net):
    net.names["example.com"] = [PUBLIC]
    net.pages[(PUBLIC, "/prices")] = page()

    got = url_fetch.fetch("https://example.com/prices")

    assert got.status == url_fetch.OK
    assert got.body == b"<p>Prices</p>"
    assert got.content_type == "text/html"
    assert net.connected == [PUBLIC]
    # The request names the host, as a browser's would.
    assert net.requests[0].headers["host"] == "example.com"


UNREACHABLE = [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://10.0.0.5/",
    "http://172.16.3.4/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[fd00:ec2::254]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",
    "https://example.com:8443/",
    "http://example.com:22/",
    "ftp://example.com/",
    "file:///etc/passwd",
    "not a url",
]


@pytest.mark.parametrize("url", UNREACHABLE)
def test_private_loopback_link_local_metadata_and_odd_ports_are_refused(net, url):
    net.names.update(
        {
            "example.com": [PUBLIC],
            "localhost": ["127.0.0.1"],
            "metadata.google.internal": ["169.254.169.254"],
        }
    )
    with pytest.raises(url_fetch.Unreachable) as refused:
        url_fetch.fetch(url)
    assert str(refused.value) == url_fetch.UNREACHABLE
    assert net.connected == []


def test_a_name_with_any_private_answer_is_refused(net):
    net.names["mixed.example"] = [PUBLIC, "10.1.2.3"]
    with pytest.raises(url_fetch.Unreachable):
        url_fetch.fetch("http://mixed.example/")
    net.names["nowhere.example"] = []
    with pytest.raises(url_fetch.Unreachable):
        url_fetch.check("http://nowhere.example/")
    assert net.connected == []


def test_a_redirect_to_a_private_address_is_refused(net):
    net.names.update({"example.com": [PUBLIC], "inside.example": ["10.0.0.7"]})
    net.pages[(PUBLIC, "/go")] = httpx.Response(302, headers={"location": "http://inside.example/"})
    net.pages[(PUBLIC, "/meta")] = httpx.Response(
        301, headers={"location": "http://169.254.169.254/latest/"}
    )

    for path in ("/go", "/meta"):
        with pytest.raises(url_fetch.Unreachable):
            url_fetch.fetch(f"http://example.com{path}")

    assert net.connected == [PUBLIC, PUBLIC]


def test_a_redirect_to_a_public_address_is_followed(net):
    net.names.update({"example.com": [PUBLIC], "www.example.com": [OTHER_PUBLIC]})
    net.pages[(PUBLIC, "/old")] = httpx.Response(
        301, headers={"location": "https://www.example.com/new"}
    )
    net.pages[(OTHER_PUBLIC, "/new")] = page(b"moved")

    got = url_fetch.fetch("http://example.com/old")

    assert got.body == b"moved"
    assert got.url == "https://www.example.com/new"
    assert net.connected == [PUBLIC, OTHER_PUBLIC]


def test_a_dns_answer_that_changes_between_check_and_connect_never_reaches_the_new_one(net):
    # Rebinding: public when checked, private a moment later.
    net.names["rebind.example"] = [[PUBLIC], ["127.0.0.1"]]
    net.pages[(PUBLIC, "/")] = page(b"public")

    url_fetch.check("http://rebind.example/")
    with pytest.raises(url_fetch.Unreachable):
        url_fetch.fetch("http://rebind.example/")

    net.names["rebind2.example"] = [[PUBLIC], ["127.0.0.1"]]
    got = url_fetch.fetch("http://rebind2.example/")
    assert got.body == b"public"
    assert "127.0.0.1" not in net.connected


def test_redirects_past_the_limit_are_refused(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "url_fetch_max_redirects", 2)
    net.names["example.com"] = [PUBLIC]
    for n in range(4):
        net.pages[(PUBLIC, f"/{n}")] = httpx.Response(302, headers={"location": f"/{n + 1}"})
    net.pages[(PUBLIC, "/4")] = page(b"end")
    net.pages[(PUBLIC, "/a")] = httpx.Response(302, headers={"location": "/b"})
    net.pages[(PUBLIC, "/b")] = page(b"two hops")

    with pytest.raises(url_fetch.FetchFailed, match="redirects"):
        url_fetch.fetch("http://example.com/0")
    assert url_fetch.fetch("http://example.com/a").body == b"two hops"


def test_a_body_past_the_size_limit_is_refused(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "url_fetch_max_bytes", 10)
    net.names["example.com"] = [PUBLIC]
    net.pages[(PUBLIC, "/big")] = page(b"x" * 11)

    def chunked(request):
        return httpx.Response(200, content=iter([b"x" * 6, b"x" * 6]))

    net.pages[(PUBLIC, "/stream")] = chunked
    net.pages[(PUBLIC, "/small")] = page(b"x" * 10)

    for path in ("/big", "/stream"):
        with pytest.raises(url_fetch.FetchFailed, match="too large"):
            url_fetch.fetch(f"http://example.com{path}")
    assert url_fetch.fetch("http://example.com/small").body == b"x" * 10


def test_a_fetch_past_the_time_limit_is_refused(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "url_fetch_timeout_seconds", 0.2)
    net.names["example.com"] = [PUBLIC]

    def slow():
        for _ in range(5):
            time.sleep(0.1)
            yield b"tick"

    net.pages[(PUBLIC, "/slow")] = lambda request: httpx.Response(200, content=slow())

    with pytest.raises(url_fetch.FetchFailed, match="took too long"):
        url_fetch.fetch("http://example.com/slow")


def test_conditional_requests_and_removal(net):
    net.names["example.com"] = [PUBLIC]

    def conditional(request):
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return page(b"v1", etag='"v1"', **{"last-modified": "Wed, 11 Nov 2026 10:00:00 GMT"})

    net.pages[(PUBLIC, "/p")] = conditional
    net.pages[(PUBLIC, "/gone")] = httpx.Response(410)

    first = url_fetch.fetch("http://example.com/p")
    assert (first.etag, first.last_modified) == ('"v1"', "Wed, 11 Nov 2026 10:00:00 GMT")
    again = url_fetch.fetch("http://example.com/p", etag=first.etag)
    assert again.status == url_fetch.NOT_MODIFIED
    assert net.requests[-1].headers["if-none-match"] == '"v1"'

    assert url_fetch.fetch("http://example.com/missing").status == url_fetch.GONE
    assert url_fetch.fetch("http://example.com/gone").status == url_fetch.GONE
    net.pages[(PUBLIC, "/err")] = httpx.Response(500)
    with pytest.raises(url_fetch.FetchFailed, match="500"):
        url_fetch.fetch("http://example.com/err")
