"""A fake GitHub that replays recorded responses (`recordings/github.json`): the test seam for the
GitHub provider (`aios.source_providers.github`). It's an httpx transport, handed to the provider
in place of the network, so nothing leaves the machine.

It starts as the recording does: the aios test App installed on `acme` with two repositories
(`acme/api`, public, and `acme/secret-plans`, private), their issues, pull requests, comments,
review comments and `acme/api`'s files. A test changes it as GitHub would change: edit or add an
issue or comment (`put_issue`, `put_comment`), transfer or delete one (`transfer`, `delete`),
change a file (`put_file`, `delete_file`), add a repository to the installation or take one off
(`install`, `uninstall`), rate-limit the next call (`rate_limit`), or revoke the App (`revoked`).

It checks what GitHub checks: the App's JWT (signed with the App's private key, for the app id),
installation tokens it minted (expired after `expire_tokens`), and the user token from the OAuth
code it issued. It notes what it was asked (`requests`), the tokens it minted (`minted`), the
comments and issues posted (`posted`), and installations deleted (`uninstalled`).
"""

import base64
import copy
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

RECORDED = json.loads((Path(__file__).parent / "recordings" / "github.json").read_text())

APP_ID = "123456"
CLIENT_ID = "Iv1.recordedclient00"
CLIENT_SECRET = "recorded-client-secret"
SLUG = RECORDED["installation"]["app_slug"]
INSTALLATION_ID = RECORDED["installation"]["id"]
CODE = "recorded-oauth-code"


def git_sha(content: str) -> str:
    data = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


@dataclass
class FakeGitHub:
    private_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    repositories: dict[str, dict] = field(default_factory=dict)
    # The repositories the App is installed on (others 404 to its installation tokens).
    installed: list[str] = field(default_factory=list)
    issues: dict[str, dict[int, dict]] = field(default_factory=dict)
    comments: dict[str, dict[int, dict]] = field(default_factory=dict)
    review_comments: dict[str, dict[int, dict]] = field(default_factory=dict)
    # Each repository's files on its default branch: path -> content.
    files: dict[str, dict[str, str]] = field(default_factory=dict)
    # Issues moved elsewhere (301) or deleted (410), by repository and number.
    gone: dict[tuple[str, int], int] = field(default_factory=dict)
    page_size: int = 100
    revoked: bool = False
    # Rate-limits the next n calls.
    limited: int = 0
    requests: list[tuple[str, str]] = field(default_factory=list)
    minted: list[str] = field(default_factory=list)
    # Tokens that have expired.
    expired: set[str] = field(default_factory=set)
    user_tokens: list[str] = field(default_factory=list)
    posted: list[dict] = field(default_factory=list)
    uninstalled: list[int] = field(default_factory=list)
    # User installations the OAuth user can see (by default, the App's own installation).
    user_installations: list[int] = field(default_factory=lambda: [INSTALLATION_ID])
    _clock: datetime = field(default_factory=lambda: datetime(2026, 11, 1, tzinfo=UTC))

    def __post_init__(self) -> None:
        for repo in RECORDED["repositories"]:
            self.repositories[repo["full_name"]] = copy.deepcopy(repo)
            self.installed.append(repo["full_name"])
        later = RECORDED["repository_added_later"]
        self.repositories[later["full_name"]] = copy.deepcopy(later)
        for name, rows in RECORDED["issues"].items():
            self.issues[name] = {r["number"]: copy.deepcopy(r) for r in rows}
        for name, rows in RECORDED["issue_comments"].items():
            self.comments[name] = {r["id"]: copy.deepcopy(r) for r in rows}
        for name, rows in RECORDED["review_comments"].items():
            self.review_comments[name] = {r["id"]: copy.deepcopy(r) for r in rows}
        for name, entries in RECORDED["trees"].items():
            self.files[name] = {e["path"]: e["content"] for e in entries if e["type"] == "blob"}

    # --- The App's key, as aios's settings hold it --------------------------------------------

    @property
    def private_key_pem(self) -> str:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    # --- Changing it, as GitHub would change ---------------------------------------------------

    def tick(self) -> str:
        """A moment later than any before, as GitHub's `updated_at`."""
        self._clock += timedelta(minutes=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    def put_issue(self, repo: str, number: int, **fields: Any) -> dict:
        """Edit an issue (or open a new one), its `updated_at` now."""
        rows = self.issues.setdefault(repo, {})
        row = rows.get(number) or {
            "id": 990000 + number,
            "number": number,
            "title": f"Issue {number}",
            "body": "",
            "state": "open",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "user": {"login": "octocat", "id": 583231, "type": "User"},
            "labels": [],
            "comments": 0,
            "created_at": self.tick(),
        }
        if "labels" in fields:
            fields["labels"] = [{"id": i, "name": n} for i, n in enumerate(fields["labels"])]
        row.update(fields, updated_at=self.tick())
        rows[number] = row
        return row

    def put_comment(self, repo: str, number: int, comment_id: int, body: str, review=False):
        """Add or edit a comment on an issue (or, `review`, a review comment on a pull
        request)."""
        now = self.tick()
        if review:
            rows = self.review_comments.setdefault(repo, {})
            row = {
                "id": comment_id,
                "body": body,
                "path": "src/app.py",
                "html_url": f"https://github.com/{repo}/pull/{number}#discussion_r{comment_id}",
                "pull_request_url": f"https://api.github.com/repos/{repo}/pulls/{number}",
            }
        else:
            rows = self.comments.setdefault(repo, {})
            row = {
                "id": comment_id,
                "body": body,
                "html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}",
                "issue_url": f"https://api.github.com/repos/{repo}/issues/{number}",
            }
        row |= {
            "user": {"login": "monalisa", "id": 2000, "type": "User"},
            "created_at": rows.get(comment_id, {}).get("created_at", now),
            "updated_at": now,
        }
        rows[comment_id] = row
        return row

    def _drop(self, repo: str, number: int, status: int) -> None:
        self.issues.get(repo, {}).pop(number, None)
        for rows in (self.comments.get(repo, {}), self.review_comments.get(repo, {})):
            for cid, c in list(rows.items()):
                if (c.get("issue_url") or c.get("pull_request_url", "")).endswith(f"/{number}"):
                    rows.pop(cid)
        self.gone[(repo, number)] = status

    def transfer(self, repo: str, number: int) -> None:
        """Transfer the issue to another repository: it's gone from this one's listings, and
        asking for it is a 301. The change feed doesn't say."""
        self._drop(repo, number, 301)

    def delete(self, repo: str, number: int) -> None:
        """Delete the issue: gone from listings, 410 if asked for. The change feed doesn't say."""
        self._drop(repo, number, 410)

    def delete_comment(self, repo: str, comment_id: int) -> None:
        self.comments.get(repo, {}).pop(comment_id, None)
        self.review_comments.get(repo, {}).pop(comment_id, None)

    def put_file(self, repo: str, path: str, content: str) -> None:
        self.files.setdefault(repo, {})[path] = content

    def delete_file(self, repo: str, path: str) -> None:
        self.files.get(repo, {}).pop(path, None)

    def install(self, repo: str) -> None:
        """Add a repository to the installation, in GitHub."""
        if repo not in self.installed:
            self.installed.append(repo)

    def uninstall(self, repo: str) -> None:
        """Take a repository off the installation, in GitHub."""
        if repo in self.installed:
            self.installed.remove(repo)

    def rate_limit(self, calls: int = 1) -> None:
        self.limited = calls

    def expire_tokens(self) -> None:
        self.expired |= set(self.minted)

    # --- Answering ------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        if self.limited > 0:
            self.limited -= 1
            return httpx.Response(
                403,
                json=RECORDED["rate_limited"],
                headers={"x-ratelimit-remaining": "0", "retry-after": "60"},
            )
        host, path = request.url.host, request.url.path
        if host == "github.com":
            if path == "/login/oauth/access_token" and request.method == "POST":
                return self._oauth(request)
            return httpx.Response(404, json=RECORDED["not_found"])
        if host != "api.github.com":
            return httpx.Response(404, json=RECORDED["not_found"])
        if m := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
            return self._mint(request, int(m[1]))
        m = re.fullmatch(r"/app/installations/(\d+)", path)
        if m and request.method == "DELETE":
            if not self._app_jwt_ok(request):
                return httpx.Response(401, json=RECORDED["bad_credentials"])
            self.uninstalled.append(int(m[1]))
            self.revoked = True
            return httpx.Response(204)
        if path == "/user/installations":
            return self._user_installations(request)
        if not self._installation_token_ok(request):
            return httpx.Response(401, json=RECORDED["bad_credentials"])
        if path == "/installation/repositories":
            rows = [self.repositories[n] for n in self.installed]
            return self._paged(
                request, rows, lambda page: {"total_count": len(rows), "repositories": page}
            )
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if not m or m[1] not in self.installed:
            return httpx.Response(404, json=RECORDED["not_found"])
        return self._repo(request, m[1], m[2] or "")

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        ok = (
            body.get("client_id") == CLIENT_ID
            and body.get("client_secret") == CLIENT_SECRET
            and body.get("code") == CODE
        )
        if not ok:
            # GitHub answers a bad code with 200 and an error.
            return httpx.Response(200, json=RECORDED["oauth_bad_code"])
        token = f"ghu_{secrets.token_hex(16)}"
        self.user_tokens.append(token)
        return httpx.Response(200, json=RECORDED["oauth_access_token"] | {"access_token": token})

    def _user_installations(self, request: httpx.Request) -> httpx.Response:
        if _bearer(request) not in self.user_tokens:
            return httpx.Response(401, json=RECORDED["bad_credentials"])
        rows = [RECORDED["installation"] | {"id": i} for i in self.user_installations]
        return self._paged(
            request, rows, lambda page: {"total_count": len(rows), "installations": page}
        )

    def _app_jwt_ok(self, request: httpx.Request) -> bool:
        token = _bearer(request) or ""
        try:
            head, claims, signature = token.split(".")
            self.private_key.public_key().verify(
                _b64url_decode(signature),
                f"{head}.{claims}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:
            return False
        header = json.loads(_b64url_decode(head))
        payload = json.loads(_b64url_decode(claims))
        now = datetime.now(UTC).timestamp()
        return (
            header.get("alg") == "RS256"
            and str(payload.get("iss")) == APP_ID
            and payload["iat"] <= now
            and payload["exp"] > now
            and payload["exp"] - payload["iat"] <= 600
        )

    def _mint(self, request: httpx.Request, installation_id: int) -> httpx.Response:
        if request.method != "POST" or not self._app_jwt_ok(request):
            return httpx.Response(401, json=RECORDED["bad_credentials"])
        if installation_id != INSTALLATION_ID or self.revoked:
            return httpx.Response(404, json=RECORDED["not_found"])
        token = f"ghs_{secrets.token_hex(18)}"
        self.minted.append(token)
        expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return httpx.Response(
            201, json=RECORDED["access_token"] | {"token": token, "expires_at": expires}
        )

    def _installation_token_ok(self, request: httpx.Request) -> bool:
        token = _bearer(request)
        return not self.revoked and token in self.minted and token not in self.expired

    def _paged(self, request: httpx.Request, rows: list, wrap=lambda page: page) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        size = min(int(query.get("per_page", ["30"])[0]), self.page_size)
        page = int(query.get("page", ["1"])[0])
        chunk = rows[(page - 1) * size : page * size]
        headers = {}
        if page * size < len(rows):
            nxt = request.url.copy_merge_params({"page": str(page + 1)})
            headers["link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=wrap(chunk), headers=headers)

    def _repo(self, request: httpx.Request, repo: str, rest: str) -> httpx.Response:
        query = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        since = query.get("since")

        def recent(rows):
            rows = sorted(rows, key=lambda r: (r["updated_at"], r.get("id", 0)))
            return [r for r in rows if since is None or r["updated_at"] >= since]

        if rest == "" and request.method == "GET":
            return httpx.Response(200, json=self.repositories[repo])
        if rest == "/issues" and request.method == "GET":
            assert query.get("state") == "all"
            return self._paged(request, recent(self.issues.get(repo, {}).values()))
        if rest == "/issues" and request.method == "POST":
            body = json.loads(request.content)
            self.posted.append({"kind": "issue", "repository": repo, **body})
            return httpx.Response(201, json=RECORDED["created_issue"] | {"title": body["title"]})
        if rest == "/issues/comments" and request.method == "GET":
            return self._paged(request, recent(self.comments.get(repo, {}).values()))
        if rest == "/pulls/comments" and request.method == "GET":
            return self._paged(request, recent(self.review_comments.get(repo, {}).values()))
        if m := re.fullmatch(r"/issues/(\d+)/comments", rest):
            number = int(m[1])
            if (repo, number) in self.gone:
                status = self.gone[(repo, number)]
                key = "moved_permanently" if status == 301 else "issue_deleted"
                return httpx.Response(status, json=RECORDED[key])
            if number not in self.issues.get(repo, {}):
                return httpx.Response(404, json=RECORDED["not_found"])
            body = json.loads(request.content)
            self.posted.append({"kind": "comment", "repository": repo, "number": number, **body})
            return httpx.Response(201, json=RECORDED["created_comment"] | {"body": body["body"]})
        if m := re.fullmatch(r"/issues/(\d+)", rest):
            number = int(m[1])
            if (repo, number) in self.gone:
                status = self.gone[(repo, number)]
                key = "moved_permanently" if status == 301 else "issue_deleted"
                return httpx.Response(status, json=RECORDED[key])
            row = self.issues.get(repo, {}).get(number)
            return (
                httpx.Response(200, json=row)
                if row
                else httpx.Response(404, json=RECORDED["not_found"])
            )
        if m := re.fullmatch(r"/git/trees/([^/]+)", rest):
            if m[1] != self.repositories[repo]["default_branch"]:
                return httpx.Response(404, json=RECORDED["not_found"])
            tree = [
                {"path": p, "mode": "100644", "type": "blob", "sha": git_sha(c), "size": len(c)}
                for p, c in sorted(self.files.get(repo, {}).items())
            ]
            return httpx.Response(200, json={"sha": "f" * 40, "tree": tree, "truncated": False})
        if m := re.fullmatch(r"/git/blobs/([0-9a-f]{40})", rest):
            for content in self.files.get(repo, {}).values():
                if git_sha(content) == m[1]:
                    encoded = base64.b64encode(content.encode()).decode()
                    return httpx.Response(
                        200,
                        json={
                            "sha": m[1],
                            "encoding": "base64",
                            "content": encoded,
                            "size": len(content),
                        },
                    )
            return httpx.Response(404, json=RECORDED["not_found"])
        return httpx.Response(404, json=RECORDED["not_found"])


def _bearer(request: httpx.Request) -> str | None:
    auth = request.headers.get("authorization", "")
    for prefix in ("Bearer ", "bearer ", "token "):
        if auth.startswith(prefix):
            return auth[len(prefix) :]
    return None
