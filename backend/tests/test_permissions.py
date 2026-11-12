"""Roles and permissions (`aios.permissions`): every route of an organisation, called by each of
the three roles, is allowed or refused as the spec's permission table says; the admin manages
roles and invitation links' roles; an organisation always keeps an admin; participants view
and download documents but don't upload or delete them, and can't mention agents yet."""

import re
import uuid

import pytest
from conftest import (
    add_document,
    delete_user,
    join_organisation,
    org_of,
    provisioned_user,
    signed_in_client,
    store_of,
)
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import select, update
from test_chats import signed_in_as
from test_invitations import leave_behind, make_link
from test_people_mirror import mirror

from aios.api import search as search_api
from aios.db import SessionLocal
from aios.main import app
from aios.models import OrganisationMember, Role, User

ADMIN, EDITOR, PARTICIPANT = Role.admin, Role.editor, Role.participant
EVERYONE = {ADMIN, EDITOR, PARTICIPANT}
BUILDERS = {ADMIN, EDITOR}
ADMINS = {ADMIN}

# The spec's permission table, by row: who may, and a word the refusal names the action by.
READ = (EVERYONE, None)  # Read and search everything in the organisation
POST = (EVERYONE, None)  # Post, mention, and start runs
BUILD = (BUILDERS, "agents, skills, workflows and schedules")
DOCUMENTS = (BUILDERS, "documents")
DECIDE = (BUILDERS, "schema change")  # decide schema changes (approval rules refine it)
RUN_ORGANISATION = (ADMINS, "organisation")  # roles, invitations, name, settings, deletion
RESTORE = (ADMINS, "restore")  # restores need an admin
SOURCES = (ADMINS, "sources")  # add, change, pause or remove sources
LEAVE = (EVERYONE, None)
AUDIT = (ADMINS, "audit log")  # read the audit log
USAGE = (ADMINS, "usage")  # read usage totals
RELEASE = (ADMINS, "flagged")  # see flagged content, release it
TABLES = (BUILDERS, "data tables")  # share, unshare, transfer (approval rules refine it)

# Every route under the organisation prefix, by method and path after it, and its row.
ROUTES = {
    ("GET", "/agents"): READ,
    ("GET", "/approval-rules"): READ,
    ("PUT", "/approval-rules/{kind}"): RUN_ORGANISATION,
    ("GET", "/audit-log"): AUDIT,
    ("GET", "/usage"): USAGE,
    ("GET", "/flagged"): RELEASE,
    ("POST", "/flagged/{document_id}/release"): RELEASE,
    ("GET", "/jev-status"): READ,
    ("POST", "/agents"): BUILD,
    ("GET", "/agents/{agent_id}"): READ,
    ("PUT", "/agents/{agent_id}"): BUILD,
    ("POST", "/agents/{agent_id}/archive"): BUILD,
    ("POST", "/agents/{agent_id}/unarchive"): BUILD,
    ("GET", "/agents/{agent_id}/used-by"): READ,
    ("GET", "/agents/{agent_id}/export"): BUILD,
    ("GET", "/agents/{agent_id}/versions"): READ,
    ("GET", "/agents/{agent_id}/versions/{number}"): READ,
    ("POST", "/agents/{agent_id}/versions/{number}/restore"): BUILD,
    ("GET", "/agents/{agent_id}/runs"): READ,
    ("POST", "/agents/{agent_id}/runs"): POST,
    ("GET", "/agents/{agent_id}/runs/{run_id}"): READ,
    ("DELETE", "/agents/{agent_id}/runs/{run_id}"): BUILD,
    ("GET", "/agents/{agent_id}/memories"): READ,
    ("PATCH", "/agents/{agent_id}/memories/{memory_id}"): BUILD,
    ("DELETE", "/agents/{agent_id}/memories/{memory_id}"): BUILD,
    ("GET", "/chats"): READ,
    ("POST", "/chats"): POST,
    ("GET", "/chats/{chat_id}"): READ,
    ("PUT", "/chats/{chat_id}"): POST,
    ("DELETE", "/chats/{chat_id}"): POST,
    ("POST", "/chats/{chat_id}/join"): POST,
    ("POST", "/chats/{chat_id}/leave"): POST,
    ("POST", "/chats/{chat_id}/members"): POST,
    ("DELETE", "/chats/{chat_id}/members/{member_id}"): POST,
    ("GET", "/chats/{chat_id}/mentionable"): READ,
    ("GET", "/chats/{chat_id}/messages"): READ,
    ("POST", "/chats/{chat_id}/messages"): POST,
    ("POST", "/chats/{chat_id}/read"): POST,
    ("GET", "/data-store"): READ,
    ("GET", "/data/tables"): READ,
    ("GET", "/data/tables/{table:path}/rows"): READ,
    ("GET", "/data/tables/{table:path}/value"): READ,
    ("PUT", "/data/tables/{table:path}/owner"): TABLES,
    ("POST", "/data/tables/{table:path}/shares"): TABLES,
    ("DELETE", "/data/tables/{table:path}/shares/{agent_id}"): TABLES,
    ("GET", "/documents"): READ,
    ("POST", "/documents"): DOCUMENTS,
    ("DELETE", "/documents/{document_id}"): DOCUMENTS,
    ("GET", "/documents/{document_id}/download"): READ,
    ("GET", "/search"): READ,
    ("GET", "/sources"): READ,
    ("GET", "/sources/{source_id}"): READ,
    ("PATCH", "/sources/{source_id}"): SOURCES,
    ("DELETE", "/sources/{source_id}"): SOURCES,
    ("GET", "/dropped"): READ,
    ("POST", "/dropped/{dropped_id}/restore"): RESTORE,
    ("GET", "/organisation"): READ,
    ("PUT", "/organisation"): RUN_ORGANISATION,
    ("PUT", "/organisation/retention"): RUN_ORGANISATION,
    ("PUT", "/organisation/limits"): RUN_ORGANISATION,
    ("POST", "/organisation/deletion"): RUN_ORGANISATION,
    ("DELETE", "/organisation/deletion"): RUN_ORGANISATION,
    ("GET", "/organisation/invites"): RUN_ORGANISATION,
    ("POST", "/organisation/invites"): RUN_ORGANISATION,
    ("DELETE", "/organisation/invites/{invite_id}"): RUN_ORGANISATION,
    ("PUT", "/organisation/members/{user_id}/role"): RUN_ORGANISATION,
    ("PUT", "/organisation/in-flight-cap"): RUN_ORGANISATION,
    ("POST", "/organisation/leave"): LEAVE,
    ("GET", "/schema-changes"): READ,
    ("GET", "/schema-changes/{request_id}"): READ,
    ("GET", "/schema-changes/{request_id}/impact"): READ,
    ("POST", "/schema-changes/{request_id}/approve"): DECIDE,
    ("POST", "/schema-changes/{request_id}/reject"): DECIDE,
    ("GET", "/skills"): READ,
    ("POST", "/skills"): BUILD,
    ("GET", "/skills/{skill_id}"): READ,
    ("PUT", "/skills/{skill_id}"): BUILD,
    ("POST", "/skills/{skill_id}/archive"): BUILD,
    ("POST", "/skills/{skill_id}/unarchive"): BUILD,
    ("GET", "/skills/{skill_id}/used-by"): READ,
    ("GET", "/skills/{skill_id}/versions"): READ,
    ("GET", "/skills/{skill_id}/versions/{number}"): READ,
    ("POST", "/skills/{skill_id}/versions/{number}/restore"): BUILD,
    ("GET", "/triggers"): READ,
    ("POST", "/triggers"): BUILD,
    ("POST", "/triggers/preview"): READ,
    ("GET", "/triggers/{trigger_id}"): READ,
    ("PUT", "/triggers/{trigger_id}"): BUILD,
    ("DELETE", "/triggers/{trigger_id}"): BUILD,
    ("GET", "/workflows"): READ,
    ("POST", "/workflows"): BUILD,
    ("GET", "/workflows/{workflow_id}"): READ,
    ("PUT", "/workflows/{workflow_id}"): BUILD,
    ("POST", "/workflows/{workflow_id}/archive"): BUILD,
    ("POST", "/workflows/{workflow_id}/unarchive"): BUILD,
    ("GET", "/workflows/{workflow_id}/used-by"): READ,
    ("GET", "/workflows/{workflow_id}/export"): BUILD,
    ("POST", "/imports"): BUILD,
    ("GET", "/workflows/{workflow_id}/versions"): READ,
    ("GET", "/workflows/{workflow_id}/versions/{number}"): READ,
    ("POST", "/workflows/{workflow_id}/versions/{number}/restore"): BUILD,
    ("GET", "/workflows/{workflow_id}/runs"): READ,
    ("POST", "/workflows/{workflow_id}/runs"): POST,
    ("GET", "/workflows/{workflow_id}/runs/{run_id}"): READ,
    ("DELETE", "/workflows/{workflow_id}/runs/{run_id}"): BUILD,
    ("GET", "/workflows/{workflow_id}/runs/{run_id}/subworkflow"): READ,
}

PREFIX = "/api/o/{organisation_id}"


def organisation_routes() -> set[tuple[str, str]]:
    return {
        (method, r.path.removeprefix(PREFIX))
        for r in iter_route_contexts(app.router.routes)
        if isinstance(r.original_route, APIRoute) and r.path.startswith(PREFIX)
        for method in r.methods
    }


def test_the_table_covers_every_route_of_an_organisation():
    assert organisation_routes() == set(ROUTES)


@pytest.fixture(scope="module")
def roles(files_bucket):
    """An organisation with a ready store and a member in each role, by role."""
    with SessionLocal() as db:
        admin = provisioned_user(db, "admin")
        members = {ADMIN: admin}
        for role in (EDITOR, PARTICIPANT):
            u = join_organisation(db, admin, role.value)
            set_role(db, u, role)
            members[role] = u
    yield members
    with SessionLocal() as db:
        for role in (EDITOR, PARTICIPANT):
            leave_behind(db, members[role])
        delete_user(db, db.get_one(User, admin.id))


def set_role(db, u: User, role: Role, of: User | None = None) -> None:
    org_id = org_of(db, of or u).id
    db.execute(
        update(OrganisationMember)
        .where(OrganisationMember.organisation_id == org_id, OrganisationMember.user_id == u.id)
        .values(role=role)
    )
    db.commit()


def ensure_member(admin: User, u: User, role: Role) -> None:
    """Put `u` back in `admin`'s organisation with `role`, should a call have taken them out."""
    with SessionLocal() as db:
        org_id = org_of(db, admin).id
        found = db.scalar(
            select(OrganisationMember).where(
                OrganisationMember.organisation_id == org_id, OrganisationMember.user_id == u.id
            )
        )
        if found is None:
            db.add(OrganisationMember(organisation_id=org_id, user_id=u.id, role=role))
        else:
            found.role = role
        db.commit()


def fill(path: str) -> str:
    """A path with each parameter given an id that names nothing."""

    def value(match: re.Match) -> str:
        name = match.group(1)
        if name in ("request_id", "invite_id"):
            return str(uuid.uuid4())
        if name == "table:path":
            return "public.nothing"
        return "987654"

    return re.sub(r"\{([^}]+)\}", value, path)


@pytest.mark.parametrize("role", [ADMIN, EDITOR, PARTICIPANT], ids=str)
@pytest.mark.parametrize("route", sorted(ROUTES), ids=lambda r: f"{r[0]} {r[1]}")
def test_each_role_may_call_a_route_as_the_table_says(roles, role, route):
    method, path = route
    allowed, names = ROUTES[route]
    with SessionLocal() as db:
        org_id = org_of(db, roles[ADMIN]).id
    client = signed_in_client(roles[role])
    url = f"/api/o/{org_id}{fill(path)}"
    resp = client.request(method, url, json={} if method in ("POST", "PUT") else None)
    try:
        if role in allowed:
            assert resp.status_code != 403, resp.text
        else:
            assert resp.status_code == 403, resp.text
            detail = resp.json()["detail"]
            assert names in detail
            assert "Participants" in detail if role == PARTICIPANT else "Editors" in detail
    finally:
        if role != ADMIN:
            ensure_member(roles[ADMIN], roles[role], role)


# Who am I, and the Members tab.


@pytest.fixture
def member(db, user):
    """An editor in `user`'s organisation, who joined after its creator."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    leave_behind(db, u)


def test_who_am_i_says_the_persons_role_in_each_organisation(api, db, user, member):
    set_role(db, member, PARTICIPANT, of=user)
    assert [o["role"] for o in api.get("/api/me").json()["organisations"]] == ["admin"]
    with signed_in_as(member):
        listed = api.get("/api/me").json()["organisations"]
    assert [(o["id"], o["role"]) for o in listed] == [(org_of(db, user).id, "participant")]


def test_the_creator_is_admin_and_a_joiner_editor(api, user, member):
    org = api.get("/api/organisation").json()
    assert org["role"] == "admin"
    assert {m["user"]["id"]: m["role"] for m in org["members"]} == {
        user.id: "admin",
        member.id: "editor",
    }


def test_an_admin_changes_a_members_role_and_it_reaches_the_people_mirror(api, db, user, member):
    resp = api.put(f"/api/organisation/members/{member.id}/role", json={"role": "participant"})
    assert resp.status_code == 200, resp.text
    assert {m["user"]["id"]: m["role"] for m in resp.json()["members"]}[member.id] == (
        "participant"
    )
    assert mirror(db, user)[member.id][3] == "participant"
    with signed_in_as(member):
        assert api.get("/api/organisation").json()["role"] == "participant"


def test_an_admin_makes_another_admin_who_may_then_demote_the_first(api, db, user, member):
    assert api.put(f"/api/organisation/members/{member.id}/role", json={"role": "admin"}).is_success
    with signed_in_as(member):
        resp = api.put(f"/api/organisation/members/{user.id}/role", json={"role": "editor"})
    assert resp.status_code == 200, resp.text
    assert api.get("/api/organisation").json()["role"] == "editor"


def test_demoting_the_last_admin_answers_409(api, user, member):
    resp = api.put(f"/api/organisation/members/{user.id}/role", json={"role": "editor"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "An organisation needs an admin"
    assert api.get("/api/organisation").json()["role"] == "admin"


def test_a_role_change_for_someone_not_a_member_answers_404(api, new_user):
    other = new_user()
    resp = api.put(f"/api/organisation/members/{other.id}/role", json={"role": "editor"})
    assert resp.status_code == 404


def test_an_editor_cant_change_roles(api, member):
    with signed_in_as(member):
        resp = api.put(f"/api/organisation/members/{member.id}/role", json={"role": "admin"})
    assert resp.status_code == 403
    assert "Editors" in resp.json()["detail"]


def test_the_last_admin_cant_leave_while_others_remain(api, member):
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 409
    assert "admin" in resp.json()["detail"]


def test_an_admin_leaves_once_another_is_admin(api, db, user, member):
    api.put(f"/api/organisation/members/{member.id}/role", json={"role": "admin"})
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 200, resp.text
    # Back in, so the fixtures can tidy up.
    db.add(OrganisationMember(organisation_id=org_of(db, member).id, user_id=user.id))
    db.commit()


# Invitation links' roles.


def test_a_link_grants_editor_unless_the_admin_chose_another_role(api):
    assert make_link(api)["role"] == "editor"
    link = make_link(api, role="participant")
    assert link["role"] == "participant"
    listed = api.get("/api/organisation/invites").json()
    assert {i["id"]: i["role"] for i in listed}[link["id"]] == "participant"


def test_an_invited_person_joins_with_the_role_their_link_granted(api, db, user, new_user):
    link = make_link(api, role="participant")
    joiner = new_user()
    with signed_in_as(joiner):
        resp = api.post(f"/api/invites/{link['token']}/accept")
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "participant"
    members = {m["user"]["id"]: m["role"] for m in api.get("/api/organisation").json()["members"]}
    assert members[joiner.id] == "participant"
    # Out of `user`'s organisation again (their own goes with them).
    db.execute(
        OrganisationMember.__table__.delete().where(
            OrganisationMember.user_id == joiner.id,
            OrganisationMember.organisation_id == org_of(db, user).id,
        )
    )
    db.commit()


# Documents.


@pytest.fixture
def participant(db, user):
    u = join_organisation(db, user, "participant", first_name="Pat", last_name="Participant")
    set_role(db, u, PARTICIPANT, of=user)
    yield u
    leave_behind(db, u)


def test_a_participant_views_and_downloads_documents_but_cant_upload_or_delete_them(
    api, user, participant
):
    doc_id = add_document(store_of(user), b"# Pricing", filename="p.md")
    with signed_in_as(participant):
        assert [d["id"] for d in api.get("/api/documents").json()] == [doc_id]
        download = api.get(f"/api/documents/{doc_id}/download", follow_redirects=False)
        assert download.status_code != 403, download.text
        upload = api.post("/api/documents", files={"file": ("n.md", b"# New", "text/markdown")})
        assert upload.status_code == 403
        assert "upload or delete documents" in upload.json()["detail"]
        deleted = api.delete(f"/api/documents/{doc_id}")
        assert deleted.status_code == 403
        assert "upload or delete documents" in deleted.json()["detail"]
    assert [d["id"] for d in api.get("/api/documents").json()] == [doc_id]


def test_a_participant_searches_documents(api, user, participant, monkeypatch):
    """The search itself is `test_search`'s; here, that a participant may run one."""
    asked = []

    async def search_index(data_store_id, query, limit, filters):
        asked.append((data_store_id, query))
        return []

    monkeypatch.setattr(search_api, "search_index", search_index)
    with signed_in_as(participant):
        resp = api.get("/api/search", params={"q": "pricing"})
    assert resp.status_code == 200, resp.text
    assert asked == [(store_of(user), "pricing")]
