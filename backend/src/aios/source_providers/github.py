"""The GitHub provider: the aios GitHub App (`github_app_*` settings), unavailable without them.

**Connecting.** An admin is sent to install the App (`/apps/<slug>/installations/new`) on the
repositories they choose, and GitHub sends them back with the installation's id and an OAuth code
(the App asks for user authorisation during installation). The code is exchanged for the admin's
own GitHub token, used once to check they can see that installation (so an id typed into the
address bar can't connect someone else's), then dropped. **Only the installation id is kept** as
the connector's credential (`github_installation`); installation tokens are minted from the App's
private key when a sync or a tool needs one, kept in this process's memory until shortly before
they expire, and never stored. Removing the connector deletes the installation at GitHub.

**Scope.** The repositories the App is installed on, private ones restricted. Only those picked
in aios sync: a repository added to the installation later isn't synced until it's picked, and
one taken off the installation is skipped (its items go at the next reconciliation).

**What syncs**, per picked repository:
- issues and pull requests, as message items (`acme/api#12`), with state, labels, author and
  link;
- their comments and pull requests' review comments, as children of their issue
  (`acme/api#12/comments/51001`, `acme/api#13/review-comments/61001`);
- with the source's `docs` setting on, the README and Markdown files under `docs/` on the default
  branch, as file items (`acme/api:docs/deploying.md`), whose hash is the file's blob sha.

**Incremental sync** reads everything updated since the cursor (per repository, the newest
`updated_at` seen), and compares the default branch's docs files with those the cursor last saw:
a changed file is re-ingested, a deleted one removed, and turning `docs` off removes them all.
**Reconciliation** re-lists every issue, pull request, comment and docs file, so issues
transferred elsewhere or deleted (which the change feed never reports) are removed.

**Tools** are served by aios, calling GitHub's REST API as the App: `comment_on_issue` (an issue
or pull request) and `open_issue`. Each needs its repository on the agent's allowed list
(`aios.tools.connector`), and what it posts is signed "— <agent name> via aios".

Every call goes through `aios.outbound` (GitHub's hosts only). `TRANSPORT` replaces the network
in tests (a fake GitHub replaying recorded responses).
"""

import base64
import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from aios import outbound
from aios.config import get_settings
from aios.datastores.models import SyncedItem, SyncedItemKind
from aios.mcp_client import Tool
from aios.source_providers import (
    Changes,
    Content,
    Grant,
    Item,
    KeptDocument,
    NeedsReconnecting,
    Provider,
    ScopeOption,
    SourceView,
    Tools,
    needs_app_settings,
)

log = logging.getLogger(__name__)

NAME = "github"
TITLE = "GitHub"
API = "https://api.github.com"
WEB = "https://github.com"
PAGE = 100
# Re-mint an installation token this long before it expires.
TOKEN_MARGIN_SECONDS = 5 * 60
SIGNATURE = "\n\n— {agent} via aios"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# The network, unless a test hands GitHub's calls to a fake.
TRANSPORT: httpx.BaseTransport | None = None


class GitHubError(Exception):
    """GitHub refused a call, or couldn't answer it."""


class RateLimited(GitHubError):
    """GitHub's rate limit was reached: the sync fails, and is retried later."""


def _unavailable() -> str | None:
    s = get_settings()
    return needs_app_settings(
        TITLE,
        s.github_app_id,
        s.github_app_private_key,
        s.github_app_slug,
        s.github_app_client_id,
        s.github_app_client_secret,
    )


def _client(**headers: str) -> httpx.Client:
    return outbound.client(
        NAME,
        transport=TRANSPORT,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aios",
            **headers,
        },
    )


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message") or resp.reason_phrase)
    except ValueError:
        return resp.reason_phrase


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    if resp.status_code in (403, 429) and (
        resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers
    ):
        wait = resp.headers.get("retry-after", "a while")
        raise RateLimited(f"GitHub's rate limit was reached {what}: retry after {wait} seconds.")
    raise GitHubError(f"GitHub answered {resp.status_code} {what}: {_message(resp)}")


# --- The App's own authority -------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _app_jwt() -> str:
    """A JWT for the App, signed with its private key (RS256), good for nine minutes."""
    s = get_settings()
    key = serialization.load_pem_private_key(
        s.github_app_private_key.get_secret_value().encode(), password=None
    )
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(
        json.dumps({"iat": now - 60, "exp": now + 9 * 60, "iss": s.github_app_id}).encode()
    )
    signing = f"{header}.{claims}".encode()
    signature = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[call-arg, union-attr]
    return f"{header}.{claims}.{_b64url(signature)}"


# installation id -> (token, when it stops being used, by `time.time`). Memory only.
_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def forget_tokens() -> None:
    with _tokens_lock:
        _tokens.clear()


def _forget(installation_id: str) -> None:
    with _tokens_lock:
        _tokens.pop(installation_id, None)


def installation_token(installation_id: str) -> str:
    """A token for the installation: minted from the App's key when there's none in hand that
    lasts a while yet. Raises `NeedsReconnecting` if GitHub no longer lets aios use it."""
    with _tokens_lock:
        hit = _tokens.get(installation_id)
        if hit is not None and hit[1] > time.time():
            return hit[0]
    if not installation_id.isdigit():
        raise NeedsReconnecting("The GitHub connector's installation isn't one GitHub knows.")
    with _client(Authorization=f"Bearer {_app_jwt()}") as http:
        resp = http.post(f"{API}/app/installations/{installation_id}/access_tokens")
    if resp.status_code in (401, 403, 404):
        raise NeedsReconnecting(
            "GitHub no longer lets aios use this installation: the App was uninstalled, "
            "suspended or lost its access."
        )
    _raise_for(resp, "minting an installation token")
    body = resp.json()
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
    with _tokens_lock:
        _tokens[installation_id] = (body["token"], expires - TOKEN_MARGIN_SECONDS)
    return body["token"]


class _Api:
    """GitHub's REST API as one installation: a 401 mints a fresh token once, then gives up."""

    def __init__(self, installation_id: str | None):
        if not installation_id:
            raise NeedsReconnecting("The GitHub connector has no installation.")
        self.installation_id = installation_id

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in (1, 2):
            token = installation_token(self.installation_id)
            with _client(Authorization=f"Bearer {token}") as http:
                resp = http.request(method, url if "://" in url else API + url, **kwargs)
            if resp.status_code != 401:
                return resp
            _forget(self.installation_id)
            if attempt == 2:
                raise NeedsReconnecting("GitHub no longer accepts this installation's tokens.")
        raise AssertionError  # unreachable

    def get(self, path: str, **params: Any) -> Any:
        resp = self.request("GET", path, params=params)
        _raise_for(resp, f"to {path}")
        return resp.json()

    def pages(self, path: str, key: str | None = None, **params: Any) -> Iterator[dict]:
        """Every row of a listing, following its `next` links."""
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": PAGE, **params}
        while url is not None:
            resp = self.request("GET", url, params=query)
            _raise_for(resp, f"to {path}")
            body = resp.json()
            yield from body[key] if key else body
            url = resp.links.get("next", {}).get("url")
            query = None  # the next link carries the query


# --- Items -------------------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _login(row: dict) -> str | None:
    return (row.get("user") or {}).get("login")


def _issue_text(repo: str, issue: dict) -> str:
    kind = "Pull request" if "pull_request" in issue else "Issue"
    state = issue.get("state") or "open"
    if (issue.get("pull_request") or {}).get("merged_at"):
        state = "merged"
    labels = ", ".join(label["name"] for label in issue.get("labels") or []) or "none"
    lines = [
        f"# {issue.get('title') or ''}",
        "",
        f"{kind} {repo}#{issue['number']} · state: {state} · labels: {labels}",
        f"Opened by @{_login(issue) or 'someone'}",
    ]
    if body := (issue.get("body") or "").strip():
        lines += ["", body]
    return "\n".join(lines) + "\n"


def _number_of(url: str | None) -> int | None:
    found = re.search(r"/(?:issues|pulls)/(\d+)$", url or "")
    return int(found[1]) if found else None


class GitHubAdapter:
    """The adapter (`aios.source_providers.Adapter`) for a GitHub connector's source. One is made
    per sync: what its listing or change feed read is kept for the fetches that follow."""

    def __init__(self) -> None:
        self._texts: dict[str, str] = {}
        self._blobs: dict[str, str] = {}

    # --- What's there

    def _installed(self, api: _Api) -> dict[str, dict]:
        rows = api.pages("/installation/repositories", "repositories")
        return {r["full_name"]: r for r in rows}

    def _scoped(self, api: _Api, source: SourceView) -> list[dict]:
        """The picked repositories the App is still installed on."""
        installed = self._installed(api)
        picked = source.settings.get("scope") or []
        for name in picked:
            if name not in installed:
                log.info("source %s: %s is no longer in the installation", source.id, name)
        return [installed[n] for n in picked if n in installed]

    def scope_options(self, source: SourceView) -> list[ScopeOption]:
        api = _Api(source.credential)
        return [
            ScopeOption(name, name, bool(repo.get("private")))
            for name, repo in sorted(self._installed(api).items())
        ]

    # --- Issues, pull requests and comments

    def _threads(self, api: _Api, repo: str, since: str | None) -> tuple[list[Item], str | None]:
        """The repository's issues and pull requests, then their comments, updated since
        `since` (everything if None); and the newest `updated_at` among them."""
        params: dict[str, Any] = {"sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        newest = since
        issues: list[Item] = []
        comments: list[Item] = []
        for issue in api.pages(f"/repos/{repo}/issues", state="all", **params):
            external_id = f"{repo}#{issue['number']}"
            text = _issue_text(repo, issue)
            self._texts[external_id] = text
            issues.append(
                Item(
                    external_id=external_id,
                    kind=SyncedItemKind.message,
                    title=f"{repo}#{issue['number']}: {issue.get('title') or ''}",
                    content_hash=_hash(text),
                    url=issue.get("html_url"),
                    author_name=_login(issue),
                    updated_at=_when(issue.get("updated_at")),
                )
            )
            newest = max(newest or "", issue.get("updated_at") or "") or None
        feeds = (
            ("comments", "Comment", "issue_url", f"/repos/{repo}/issues/comments"),
            (
                "review-comments",
                "Review comment",
                "pull_request_url",
                f"/repos/{repo}/pulls/comments",
            ),
        )
        for segment, label, parent_url, path in feeds:
            for c in api.pages(path, **params):
                number = _number_of(c.get(parent_url))
                if number is None:
                    continue
                external_id = f"{repo}#{number}/{segment}/{c['id']}"
                body = c.get("body") or ""
                where = f" ({c['path']})" if c.get("path") else ""
                self._texts[external_id] = body
                comments.append(
                    Item(
                        external_id=external_id,
                        kind=SyncedItemKind.message,
                        title=f"{label} on {repo}#{number}{where}",
                        content_hash=_hash(body),
                        url=c.get("html_url"),
                        author_name=_login(c),
                        updated_at=_when(c.get("updated_at")),
                        parent_external_id=f"{repo}#{number}",
                    )
                )
                newest = max(newest or "", c.get("updated_at") or "") or None
        # Parents before their children, so each comment finds its issue.
        return issues + comments, newest

    # --- Docs files

    def _docs(self, api: _Api, repo: dict) -> dict[str, str]:
        """The README and Markdown files under docs/ on the default branch: path -> blob sha."""
        name, branch = repo["full_name"], repo.get("default_branch") or "main"
        resp = api.request("GET", f"/repos/{name}/git/trees/{branch}", params={"recursive": "1"})
        if resp.status_code in (404, 409):  # an empty repository has no tree
            return {}
        _raise_for(resp, f"to the {name} tree")
        found = {}
        for entry in resp.json().get("tree") or []:
            path = entry.get("path") or ""
            if entry.get("type") != "blob":
                continue
            readme = (
                "/" not in path
                and path.lower().startswith("readme")
                and path.lower().endswith(".md")
            )
            doc = path.startswith("docs/") and path.lower().endswith((".md", ".markdown"))
            if readme or doc:
                found[path] = entry["sha"]
        return found

    def _doc_item(self, repo: dict, path: str, sha: str) -> Item:
        name, branch = repo["full_name"], repo.get("default_branch") or "main"
        external_id = f"{name}:{path}"
        self._blobs[external_id] = name
        return Item(
            external_id=external_id,
            kind=SyncedItemKind.file,
            title=f"{name}/{path}",
            content_hash=sha,
            url=f"{WEB}/{name}/blob/{branch}/{path}",
            content_type="text/markdown",
        )

    # --- The adapter

    def changes(self, source: SourceView, cursor: str | None) -> Changes:
        api = _Api(source.credential)
        at = (json.loads(cursor) if cursor else {}).get("repos", {})
        docs_on = bool(source.settings.get("docs"))
        changed: list[Item] = []
        removed: list[str] = []
        state: dict[str, dict] = {}
        for repo in self._scoped(api, source):
            name = repo["full_name"]
            before = at.get(name, {})
            threads, newest = self._threads(api, name, before.get("since"))
            changed += threads
            seen = before.get("docs") or {}
            docs = None
            if docs_on:
                docs = self._docs(api, repo)
                changed += [
                    self._doc_item(repo, p, sha) for p, sha in docs.items() if seen.get(p) != sha
                ]
                removed += [f"{name}:{p}" for p in seen if p not in docs]
            else:
                removed += [f"{name}:{p}" for p in seen]
            state[name] = {"since": newest, "docs": docs}
        return Changes(changed, removed, json.dumps({"repos": state}, sort_keys=True))

    def listing(self, source: SourceView) -> Changes:
        api = _Api(source.credential)
        docs_on = bool(source.settings.get("docs"))
        listed: list[Item] = []
        state: dict[str, dict] = {}
        for repo in self._scoped(api, source):
            name = repo["full_name"]
            threads, newest = self._threads(api, name, None)
            listed += threads
            docs = None
            if docs_on:
                docs = self._docs(api, repo)
                listed += [self._doc_item(repo, p, sha) for p, sha in docs.items()]
            state[name] = {"since": newest, "docs": docs}
        return Changes(listed, [], json.dumps({"repos": state}, sort_keys=True))

    def fetch(self, source: SourceView, item: Item) -> Content:
        if item.kind == SyncedItemKind.message:
            if item.external_id not in self._texts:
                raise GitHubError(f"{item.external_id} wasn't in this sync's reading of GitHub.")
            return Content(text=self._texts[item.external_id])
        repo = self._blobs.get(item.external_id) or item.external_id.split(":", 1)[0]
        blob = _Api(source.credential).get(f"/repos/{repo}/git/blobs/{item.content_hash}")
        return Content(data=base64.b64decode(blob.get("content") or ""))


# --- Connecting --------------------------------------------------------------------------------


def _authorise_url(state: str) -> str:
    slug = get_settings().github_app_slug
    return f"{WEB}/apps/{slug}/installations/new?" + urlencode({"state": state})


def _exchange(code: str, installation_id: str | None = None) -> Grant:
    """The installation GitHub sent the admin back with, once their own GitHub authorisation
    (`code`) shows they can see it. Raises `ValueError` if it doesn't."""
    if not installation_id or not installation_id.isdigit():
        raise ValueError("GitHub didn't say which installation was made.")
    s = get_settings()
    with _client() as http:
        resp = http.post(
            f"{WEB}/login/oauth/access_token",
            json={
                "client_id": s.github_app_client_id,
                "client_secret": s.github_app_client_secret.get_secret_value(),
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
    _raise_for(resp, "exchanging the authorisation code")
    user_token = resp.json().get("access_token")
    if not user_token:
        raise ValueError("GitHub didn't accept the authorisation code.")
    account = None
    url: str | None = f"{API}/user/installations"
    query: dict[str, Any] | None = {"per_page": PAGE}
    with _client(Authorization=f"Bearer {user_token}") as http:
        while url is not None and account is None:
            resp = http.get(url, params=query)
            _raise_for(resp, "listing the person's installations")
            for row in resp.json().get("installations") or []:
                if str(row.get("id")) == installation_id:
                    account = (row.get("account") or {}).get("login") or ""
            url, query = resp.links.get("next", {}).get("url"), None
    if account is None:
        raise ValueError("That GitHub installation isn't one the person can see.")
    return Grant(secret=installation_id, kind="github_installation", external_account=account)


def _revoke(installation_id: str) -> None:
    """Uninstall the App from the account: GitHub's way of revoking its access."""
    _forget(installation_id)
    with _client(Authorization=f"Bearer {_app_jwt()}") as http:
        resp = http.delete(f"{API}/app/installations/{installation_id}")
    if resp.status_code != 404:
        _raise_for(resp, "uninstalling the App")


# --- A removed connector's content, kept -------------------------------------------------------


def _at(row: SyncedItem) -> datetime:
    return row.source_updated_at or row.created_at


def _keep_groups(rows: Iterable[SyncedItem]) -> list[KeptDocument]:
    """One Markdown document per issue or pull request, with its comments in time order. Docs
    files are documents already, kept as they are."""
    rows = [r for r in rows if r.kind == SyncedItemKind.message]
    children: dict[int, list[SyncedItem]] = defaultdict(list)
    for r in rows:
        if r.parent_id is not None:
            children[r.parent_id].append(r)
    kept = []
    for top in sorted((r for r in rows if r.parent_id is None), key=lambda r: r.external_id):
        lines = [(top.text or "").rstrip(), ""]
        if top.url:
            lines += [f"[On GitHub]({top.url})", ""]
        for c in sorted(children.get(top.id, []), key=_at):
            head = f"**@{c.author_name or 'someone'}**, {_at(c):%Y-%m-%d %H:%M} UTC"
            if c.url:
                head += f" ([link]({c.url}))"
            lines += [f"## {c.title}", "", head, "", (c.text or "").rstrip(), ""]
        filename = re.sub(r"[\\/:*?\"<>|]+", " ", top.title or top.external_id).strip()[:150]
        kept.append(KeptDocument(f"{filename}.md", "\n".join(lines).rstrip() + "\n"))
    return kept


# --- Tools -------------------------------------------------------------------------------------

_REPOSITORY_ARG = {
    "type": "string",
    "description": "The repository, as owner/name (one this agent is allowed to act in).",
}

TOOLS = [
    Tool(
        "comment_on_issue",
        "Comment on an issue or pull request in a GitHub repository. The comment is posted by the"
        " aios GitHub App and signed with your name.",
        {
            "type": "object",
            "properties": {
                "repository": _REPOSITORY_ARG,
                "number": {"type": "integer", "description": "The issue or pull request number."},
                "body": {"type": "string", "description": "The comment, in Markdown."},
            },
            "required": ["repository", "number", "body"],
        },
    ),
    Tool(
        "open_issue",
        "Open an issue in a GitHub repository. It's opened by the aios GitHub App and signed with"
        " your name.",
        {
            "type": "object",
            "properties": {
                "repository": _REPOSITORY_ARG,
                "title": {"type": "string", "description": "The issue's title."},
                "body": {"type": "string", "description": "The issue's description, in Markdown."},
            },
            "required": ["repository", "title", "body"],
        },
    ),
]


def _listing() -> list[Tool]:
    return list(TOOLS)


def _signed(body: Any, agent: str) -> str:
    return str(body or "").rstrip() + SIGNATURE.format(agent=agent)


def _invoke(installation_id: str, tool: str, args: dict[str, Any], agent: str) -> tuple[str, bool]:
    """Call one of the tools as the App: its result's text, and whether it's an error."""
    repo = str(args.get("repository") or "")
    if not REPOSITORY.match(repo):
        return f"{repo!r} isn't a repository: name it as owner/name.", True
    try:
        api = _Api(installation_id)
        if tool == "comment_on_issue":
            number = args.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                return "The issue or pull request number must be a positive whole number.", True
            resp = api.request(
                "POST",
                f"/repos/{repo}/issues/{number}/comments",
                json={"body": _signed(args.get("body"), agent)},
            )
            _raise_for(resp, f"commenting on {repo}#{number}")
            return f"Commented on {repo}#{number}: {resp.json().get('html_url')}", False
        if tool == "open_issue":
            title = str(args.get("title") or "").strip()
            if not title:
                return "An issue needs a title.", True
            resp = api.request(
                "POST",
                f"/repos/{repo}/issues",
                json={"title": title, "body": _signed(args.get("body"), agent)},
            )
            _raise_for(resp, f"opening an issue in {repo}")
            body = resp.json()
            return f"Opened {repo}#{body.get('number')}: {body.get('html_url')}", False
    except NeedsReconnecting:
        return "The GitHub connector needs reconnecting by an admin.", True
    except (GitHubError, outbound.HostNotAllowed) as exc:
        return str(exc), True
    return f"GitHub has no tool {tool!r}.", True


PROVIDER = Provider(
    NAME,
    TITLE,
    _unavailable,
    adapter=GitHubAdapter,
    authorise_url=_authorise_url,
    exchange=_exchange,
    revoke=_revoke,
    keep_groups=_keep_groups,
    tools=Tools(
        target_args=("repository",),
        listing=_listing,
        invoke=_invoke,
        targets_required=True,
        targets_name="repositories",
    ),
    docs=True,
)
