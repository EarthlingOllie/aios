"""The organisation prefix (`/api/o/{organisation_id}/...`): a request names the organisation it
means by its id, and `aios.auth.current_organisation` checks the signed-in user is a member.

The scoping tests are generated from the app's routes, so a route added later is covered
without listing it here."""

import re
import uuid

import pytest
from conftest import delete_user, join_organisation, make_user, org_of
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_chats import signed_in_as

from aios.api import scoping
from aios.auth import current_organisation
from aios.main import app
from aios.models import Organisation

PREFIX = "/api/o/{organisation_id}"


def resolves(dependant, call) -> bool:
    """Whether `call` is anywhere in the route's dependency tree."""
    return dependant.call is call or any(resolves(d, call) for d in dependant.dependencies)


def api_routes() -> list:
    return [
        c for c in iter_route_contexts(app.router.routes) if isinstance(c.original_route, APIRoute)
    ]


def prefixed_routes() -> list[tuple[str, str, object]]:
    """(method, path, route) for every route under the prefix."""
    return [
        (method, c.path, c)
        for c in api_routes()
        if c.path.startswith(f"{PREFIX}/")
        for method in sorted(c.methods)
    ]


PREFIXED = prefixed_routes()


def path_value(dependant, name: str) -> str:
    """A well-formed value for a path parameter, so only the organisation can refuse it."""
    stack = [dependant]
    while stack:
        d = stack.pop()
        if any(p.name == name and p.field_info.annotation is uuid.UUID for p in d.path_params):
            return str(uuid.uuid4())
        stack.extend(d.dependencies)
    return "1"


def url(route, organisation_id) -> str:
    def fill(match: re.Match) -> str:
        name = match.group(1).split(":")[0]
        if name == "organisation_id":
            return str(organisation_id)
        return path_value(route.dependant, name)

    return re.sub(r"\{([^}]+)\}", fill, route.path)


@pytest.fixture
def outsider(db):
    """A user in an organisation of their own."""
    u = make_user(db, "outsider")
    yield u
    delete_user(db, u)


def test_there_are_prefixed_routes():
    assert len(PREFIXED) > 50


# The routes that stay unscoped, each for the person rather than an organisation: signing in
# (and the development picker, `/api/dev/*`, when it's on), their own profile, opening and
# accepting invitations, creating an organisation, their notifications (from every organisation
# they're in), the tool library and the list of timezones. Listed here independently of the app's
# own list (`aios.api.scoping.UNSCOPED`), which this checks.
UNSCOPED = {
    ("GET", "/api/auth/options"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/me"),
    ("PATCH", "/api/me"),
    ("PUT", "/api/me/email"),
    ("PUT", "/api/me/password"),
    ("GET", "/api/me/stream"),
    ("GET", "/api/invites/{token}"),
    ("POST", "/api/invites/{token}/accept"),
    ("POST", "/api/organisations"),
    ("GET", "/api/me/notifications"),
    ("POST", "/api/me/notifications/read"),
    ("POST", "/api/me/notifications/{notification_id}/read"),
    ("GET", "/api/tools"),
    ("GET", "/api/triggers/timezones"),
    ("GET", "/api/connectors/callback"),
    # Webhook triggers' public endpoint: callers outside, keyed by the token.
    ("POST", "/api/webhooks/{token}"),
    # The receiving mail service's: each recipient's address leads to the organisation.
    ("POST", "/api/inbound-mail"),
}
DEV = {("GET", "/api/dev/users"), ("POST", "/api/dev/session")}


def test_the_app_declares_exactly_these_routes_unscoped():
    assert scoping.UNSCOPED == UNSCOPED | DEV


def test_an_api_route_without_an_organisation_that_isnt_declared_unscoped_is_refused():
    """A route added later that forgets the organisation can't be mounted unscoped by mistake:
    the app refuses to start."""
    stray = FastAPI()
    router = APIRouter(prefix="/api")

    @router.get("/stray")
    def stray_route() -> dict:
        return {}

    stray.include_router(router)
    with pytest.raises(scoping.UnscopedRoute, match=r"GET /api/stray"):
        scoping.mount_organisation_prefix(stray)


def test_only_the_listed_routes_stay_unscoped():
    unscoped = {
        (m, c.path)
        for c in api_routes()
        if c.path.startswith("/api/")
        and not c.path.startswith(f"{PREFIX}/")
        and not c.path.startswith("/api/dev/")
        for m in c.methods
    }
    assert unscoped == UNSCOPED


def test_no_unscoped_route_resolves_the_organisation():
    for c in api_routes():
        if not c.path.startswith(f"{PREFIX}/"):
            assert not resolves(c.dependant, current_organisation), c.path


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/agents"), ("GET", "/api/chats"), ("POST", "/api/triggers/preview")],
)
def test_an_unprefixed_scoped_route_is_gone(api, method, path):
    # No transition routes: the path as it was before the prefix answers nothing. (A plain
    # client: `api` would send it under the prefix.)
    assert TestClient(app).request(method, path).status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "route"), PREFIXED, ids=[f"{m} {p}" for m, p, _ in PREFIXED]
)
def test_every_prefixed_route_resolves_the_organisation_from_the_path(method, path, route):
    assert resolves(route.dependant, current_organisation)
    # And only there: nothing else in the route takes the id from the path.
    stack = [route.dependant]
    while stack:
        d = stack.pop()
        assert "organisation_id" not in [p.name for p in d.path_params]
        stack.extend(d.dependencies)


@pytest.mark.parametrize(
    ("method", "path", "route"), PREFIXED, ids=[f"{m} {p}" for m, p, _ in PREFIXED]
)
def test_a_non_member_gets_404_as_for_an_organisation_that_doesnt_exist(
    api, db, outsider, method, path, route
):
    missing = (db.scalar(select(func.max(Organisation.id))) or 0) + 1000
    theirs = org_of(db, outsider).id
    answers = []
    for organisation_id in (theirs, missing, "not-an-id", 2**40):
        resp = api.request(method, url(route, organisation_id))
        answers.append((resp.status_code, resp.json()))
    assert answers == [(404, {"detail": "Organisation not found"})] * 4


def test_a_member_reaches_their_organisation_through_the_prefix(api, db, user):
    org = org_of(db, user)
    resp = api.get(f"/api/o/{org.id}/organisation")
    assert resp.status_code == 200, resp.text
    assert (resp.json()["id"], resp.json()["slug"]) == (org.id, org.slug)
    made = api.post(f"/api/o/{org.id}/skills", json={"name": "Prefixed", "content": "Do it."})
    assert made.status_code == 201, made.text
    assert [s["name"] for s in api.get(f"/api/o/{org.id}/skills").json()] == ["Prefixed"]


def test_another_member_reaches_it_too(api, db, user):
    member = join_organisation(db, user, "member")
    try:
        with signed_in_as(member):
            resp = api.get(f"/api/o/{org_of(db, user).id}/organisation")
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_creator"] is False
    finally:
        db.delete(member)
        db.commit()
