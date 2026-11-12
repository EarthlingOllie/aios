"""Chats without owners: every chat member is equal (renames, describes, adds and removes agents
and workflows), nobody removes a person, and admins alone archive and unarchive. An archived
chat is read-only, still read and searched, and the runs already under way in it still post."""

from dataclasses import dataclass

import pytest
from conftest import delete_user, join_organisation, store_of
from sqlalchemy import func, update
from test_chat_agent_invocations import replies_from, say
from test_chats import add, make_agent, make_chat, make_workflow, messages, post, signed_in_as
from test_open_chats import counts
from test_permissions import set_role
from test_search import embedded, found  # noqa: F401 (a fixture)

from aios import jev
from aios.api import chats
from aios.datastores import store_session
from aios.datastores.models import Chat
from aios.models import Role
from aios.testing.fake_model import replies, use_model


@pytest.fixture
def editor(db, user):
    """An editor of the first user's organisation (its creator, an admin) who hasn't joined
    their chats."""
    u = join_organisation(db, user, "editor", first_name="Ed", last_name="Itor")
    set_role(db, u, Role.editor, of=user)
    yield u
    delete_user(db, u)


@pytest.fixture
def participant(db, user):
    u = join_organisation(db, user, "participant", first_name="Pat", last_name="Icipant")
    set_role(db, u, Role.participant, of=user)
    yield u
    delete_user(db, u)


def archive(api, chat: dict):
    return api.post(f"/api/chats/{chat['id']}/archive")


def unarchive(api, chat: dict):
    return api.post(f"/api/chats/{chat['id']}/unarchive")


def test_only_admins_archive_and_unarchive(api, user, editor, participant):
    chat = make_chat(api, "Design")
    for someone in (editor, participant):
        with signed_in_as(someone):
            api.post(f"/api/chats/{chat['id']}/join")
            assert archive(api, chat).status_code == 403

    archived = archive(api, chat)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert archived.json()["archived_by"]["id"] == user.id

    for someone in (editor, participant):
        with signed_in_as(someone):
            assert unarchive(api, chat).status_code == 403
    unarchived = unarchive(api, chat)
    assert unarchived.status_code == 200, unarchived.text
    assert unarchived.json()["archived_at"] is None
    assert unarchived.json()["archived_by"] is None


def test_any_chat_member_changes_a_chat_and_one_who_hasnt_joined_cant(
    api, user, editor, participant
):
    agent = make_agent(api, "Bot")
    workflow = make_workflow(api, "Flow")
    chat = make_chat(api, "Design")
    url = f"/api/chats/{chat['id']}"

    def change(name: str) -> dict[str, int]:
        codes = {
            "rename": api.put(url, json={"name": name, "description": f"{name}'s"}).status_code,
            "add agent": api.post(
                f"{url}/members", json={"kind": "agent", "id": agent["id"]}
            ).status_code,
            "add workflow": api.post(
                f"{url}/members", json={"kind": "workflow", "id": workflow["id"]}
            ).status_code,
        }
        members = api.get(url).json()["members"]
        # What they added, else (when they couldn't add) what was there.
        added = [m["id"] for m in members["agents"] if m["target_id"] == agent["id"]] + [
            m["id"] for m in members["workflows"] if m["target_id"] == workflow["id"]
        ]
        removable = added or [members["agents"][0]["id"]]
        codes["remove"] = max(api.delete(f"{url}/members/{i}").status_code for i in removable)
        return codes

    for someone in (editor, participant):
        with signed_in_as(someone):
            # Not joined: none of it.
            assert set(change(f"Not {someone.first_name}").values()) == {403}
            assert api.get(url).json()["actions"] == ["post", "join"]
            api.post(f"{url}/join")
            assert api.get(url).json()["actions"] == ["post", "leave", "edit", "include"]
            assert change(someone.first_name) == {
                "rename": 200,
                "add agent": 201,
                "add workflow": 201,
                "remove": 204,
            }
            assert api.get(url).json()["name"] == someone.first_name

    # The admin's only extra power is archiving.
    assert api.get(url).json()["actions"] == ["post", "leave", "edit", "include", "archive"]


def test_nobody_adds_or_removes_a_person_admins_included(api, user, editor):
    chat = make_chat(api, "Design")
    url = f"/api/chats/{chat['id']}"
    with signed_in_as(editor):
        api.post(f"{url}/join")
    people = api.get(url).json()["members"]["users"]
    theirs = next(m for m in people if m["target_id"] == editor.id)
    mine = next(m for m in people if m["target_id"] == user.id)

    # The admin (the organisation's creator), and a member, each try.
    for someone in (user, editor):
        with signed_in_as(someone):
            for member in (theirs, mine):
                resp = api.delete(f"{url}/members/{member['id']}")
                assert resp.status_code == 403
                assert "Nobody removes a person" in resp.json()["detail"]
    resp = api.post(f"{url}/members", json={"kind": "user", "id": editor.id})
    assert resp.status_code == 403
    assert "Nobody adds a person" in resp.json()["detail"]
    assert {m["target_id"] for m in api.get(url).json()["members"]["users"]} == {
        user.id,
        editor.id,
    }


def test_the_last_person_leaves_and_the_chat_stays(api, user):
    chat = make_chat(api, "Design")
    post(api, chat, "Hello")
    assert api.post(f"/api/chats/{chat['id']}/leave").status_code == 204

    read = api.get(f"/api/chats/{chat['id']}").json()
    assert read["members"]["users"] == []
    assert read["actions"] == ["post", "join", "archive"]
    assert [m["body"] for m in messages(api, chat)["messages"]] == ["Hello"]


def test_an_archived_chat_refuses_every_write_without_asking_jev(api, user, editor, jev_classifier):
    agent = make_agent(api, "Bot")
    other_agent = make_agent(api, "Other")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    post(api, chat, "Before")
    assert archive(api, chat).status_code == 200
    asked = len(jev_classifier.contents)
    url = f"/api/chats/{chat['id']}"
    bot = next(m for m in api.get(url).json()["members"]["agents"] if m["name"] == "Bot")

    refused = {
        "post": api.post(f"{url}/messages", json={"body": "After", "mentions": []}),
        "mention": api.post(
            f"{url}/messages",
            json={"body": "@[Bot](agent:1) go", "mentions": [{"kind": "agent", "id": agent["id"]}]},
        ),
        "edit": api.put(url, json={"name": "Renamed", "description": ""}),
        "add": api.post(f"{url}/members", json={"kind": "agent", "id": other_agent["id"]}),
        "remove": api.delete(f"{url}/members/{bot['id']}"),
    }
    with signed_in_as(editor):
        refused["join"] = api.post(f"{url}/join")
        refused["post as a new member"] = api.post(
            f"{url}/messages", json={"body": "Hi", "mentions": []}
        )
    for what, resp in refused.items():
        assert resp.status_code == 409, what
        assert resp.json()["detail"] == chats.ARCHIVED, what
    assert len(jev_classifier.contents) == asked

    read = api.get(url).json()
    assert read["name"] == "Design"
    assert [m["name"] for m in read["members"]["agents"]] == [
        m["name"] for m in chat["members"]["agents"]
    ] + ["Bot"]
    assert [m["body"] for m in messages(api, chat)["messages"]] == ["Before"]
    # Members still leave it; the admin can only unarchive (or leave).
    assert read["actions"] == ["leave", "unarchive"]
    assert api.post(f"{url}/leave").status_code == 204


def test_archived_chats_are_listed_apart_and_still_searched(
    api,
    user,
    embedded,  # noqa: F811 (a fixture)
):
    kept = make_chat(api, "Kept")
    old = make_chat(api, "Old")
    post(api, old, "The sourdough starter lives in the fridge.")
    archive(api, old)

    assert [c["name"] for c in api.get("/api/chats").json()] == ["Kept"]
    archived = api.get("/api/chats", params={"archived": "true"}).json()
    assert [(c["name"], c["archived_by"]["id"]) for c in archived] == [("Old", user.id)]
    assert [(h["kind"], h["chat_id"]) for h in found(api, "sourdough")] == [
        ("chat_message", old["id"])
    ]

    unarchive(api, old)
    assert {c["name"] for c in api.get("/api/chats").json()} == {"Kept", "Old"}
    assert kept["archived_at"] is None


def test_a_run_under_way_when_its_chat_is_archived_still_posts(api, run_service):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    run_service.auto = False
    with use_model(replies("Done.")):
        say(api, chat, f"@[Bot](agent:{agent['id']}) go", ("agent", agent))

    assert archive(api, chat).status_code == 200
    with use_model(replies("Done.")):
        run_service.drain()

    assert replies_from(api, chat) == [("Bot", "Done.")]
    assert messages(api, chat)["invocations"][0]["status"] == "succeeded"


def test_renames_archiving_and_unarchiving_are_in_the_audit_log(api, user):
    chat = make_chat(api, "Design")
    url = f"/api/chats/{chat['id']}"
    api.put(url, json={"name": "Design", "description": "Only the description"})
    api.put(url, json={"name": "Product", "description": ""})
    archive(api, chat)
    archive(api, chat)
    unarchive(api, chat)

    entries = api.get("/api/audit-log", params={"action": "chat"}).json()["entries"]
    assert [
        (e["action"], e["actor"]["user"]["id"], e["target_kind"], e["target_id"], e["details"])
        for e in reversed(entries)
    ] == [
        (
            "chat.renamed",
            user.id,
            "chat",
            str(chat["id"]),
            {"name": "Product", "previous_name": "Design"},
        ),
        ("chat.archived", user.id, "chat", str(chat["id"]), {"name": "Product"}),
        ("chat.unarchived", user.id, "chat", str(chat["id"]), {"name": "Product"}),
    ]


def test_nobody_deletes_a_chat(api, user):
    chat = make_chat(api, "Design")
    assert api.delete(f"/api/chats/{chat['id']}").status_code == 405
    assert api.get(f"/api/chats/{chat['id']}").status_code == 200


@dataclass
class ArchivingJev(jev.FakeJev):
    """Jev, while an admin archives the chat: the archive lands between the post's first look at
    the chat and its write."""

    chat_id: int = 0
    user_id: int = 0
    store_id: object = None

    async def classify(self, content: str, source: str) -> jev.Judgement:
        with store_session(self.store_id, "rw") as store:
            store.execute(
                update(Chat)
                .where(Chat.id == self.chat_id)
                .values(archived_at=func.now(), archived_by=self.user_id)
            )
            store.commit()
        return await super().classify(content, source)


def test_a_post_that_an_archive_overtakes_is_refused_and_writes_nothing(api, user):
    chat = make_chat(api, "Design")
    before = counts(user)
    archiving = ArchivingJev(chat_id=chat["id"], user_id=user.id, store_id=store_of(user))

    with jev.use(archiving):
        resp = api.post(f"/api/chats/{chat['id']}/messages", json={"body": "Late", "mentions": []})

    assert resp.status_code == 409
    assert resp.json()["detail"] == chats.ARCHIVED
    assert counts(user) == before
