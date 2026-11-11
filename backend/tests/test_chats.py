from contextlib import contextmanager

import pytest
from conftest import delete_user, join_organisation, provisioned_user

from aios.api import chats
from aios.auth import current_user
from aios.datastores.models import ChatMessage
from aios.main import app
from aios.models import ChatAuthorKind, ChatMessageKind, User


def make_chat(api, name: str, **extra) -> dict:
    resp = api.post("/api/chats", json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def other(db, user):
    """A second member of the first user's organisation, to be another member (or not) of their
    chats. Gone first, with the organisation and what's in it."""
    u = join_organisation(db, user, "other", first_name="Other", last_name="Person")
    yield u
    delete_user(db, u)


@pytest.fixture
def outsider(db):
    """A user in an organisation of their own, with its store provisioned."""
    u = provisioned_user(db, "outsider")
    yield u
    delete_user(db, u)


@contextmanager
def signed_in_as(user: User):
    """Make the `api` client's requests come from `user` within the block."""
    previous = app.dependency_overrides[current_user]
    app.dependency_overrides[current_user] = lambda: user
    try:
        yield
    finally:
        app.dependency_overrides[current_user] = previous


def test_create_makes_the_creator_its_first_member(api, user):
    chat = make_chat(api, "Design", description="Where design happens")
    assert chat["name"] == "Design"
    assert chat["description"] == "Where design happens"
    assert "visibility" not in chat and "is_owner" not in chat
    assert chat["is_member"] is True
    # The creator, and their documents assistant (see test_chat_documents_assistant.py).
    assert chat["member_count"] == 2
    assert [a["name"] for a in chat["members"]["agents"]] == ["Documents assistant"]
    assert chat["members"]["workflows"] == []
    [member] = chat["members"]["users"]
    assert member["kind"] == "user"
    assert "role" not in member
    assert member["target_id"] == user.id
    assert member["name"] == "Test User"
    assert "login" not in member and "avatar_url" not in member
    assert chat["created_at"] == chat["updated_at"]


def test_names_are_unique_among_the_organisations_chats(api, other):
    make_chat(api, "General")
    resp = api.post("/api/chats", json={"name": "General"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The organisation already has a chat with that name."
    # Even another member's: a chat is named like a channel, not like something of one's own.
    with signed_in_as(other):
        resp = api.post("/api/chats", json={"name": "General"})
        assert resp.status_code == 409


def test_read(api):
    chat = make_chat(api, "Design", description="Pixels")
    assert api.get(f"/api/chats/{chat['id']}").json() == chat
    assert api.get(f"/api/chats/{chat['id'] + 1000}").status_code == 404


def add_user(api, chat: dict, user: User) -> dict:
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": user.id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_member_adds_a_user_as_a_member(api, user, other):
    chat = make_chat(api, "Design")
    updated = add_user(api, chat, other)
    assert updated["member_count"] == 3
    first, added = updated["members"]["users"]
    assert first["target_id"] == user.id
    assert added["target_id"] == other.id
    assert added["name"] == "Other Person"

    # Once only.
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": other.id})
    assert resp.status_code == 409
    # A user who doesn't exist, and agents and workflows that don't.
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": -1})
    assert resp.status_code == 404
    for kind in ("agent", "workflow"):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": 99_999})
        assert resp.status_code == 422, kind


def test_someone_who_hasnt_joined_cant_add_to_a_chat(api, user, other):
    chat = make_chat(api, "Design")
    with signed_in_as(other):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": other.id})
        assert resp.status_code == 403
        assert resp.json()["detail"] == chats.NOT_JOINED


def test_list_is_every_chat_most_recently_active_first(api, user, other):
    mine = make_chat(api, "Mine")
    with signed_in_as(other):
        theirs_with_me = make_chat(api, "Theirs with me")
        make_chat(api, "Theirs")
        add_user(api, theirs_with_me, user)
    listed = api.get("/api/chats").json()
    assert [c["name"] for c in listed] == ["Theirs with me", "Theirs", "Mine"]
    assert [c["is_member"] for c in listed] == [True, False, True]
    assert listed[0]["member_count"] == 3
    assert "members" not in listed[0]

    # Changing a chat makes it the most recently active.
    resp = api.put(f"/api/chats/{mine['id']}", json={"name": "Mine", "description": "Described"})
    assert resp.status_code == 200, resp.text
    assert [c["name"] for c in api.get("/api/chats").json()][0] == "Mine"


def test_only_its_members_change_or_delete_a_chat(api, user, other):
    chat = make_chat(api, "Design")
    body = {"name": "Product design", "description": "Pixels"}
    with signed_in_as(other):
        # Anyone reads it, but someone who hasn't joined can't change or delete it.
        assert api.get(f"/api/chats/{chat['id']}").status_code == 200
        resp = api.put(f"/api/chats/{chat['id']}", json=body)
        assert resp.status_code == 403
        assert resp.json()["detail"] == chats.NOT_JOINED
        assert api.delete(f"/api/chats/{chat['id']}").status_code == 403
    assert api.get(f"/api/chats/{chat['id']}").json()["name"] == "Design"

    # Any member can.
    add_user(api, chat, other)
    with signed_in_as(other):
        updated = api.put(f"/api/chats/{chat['id']}", json=body).json()
    assert updated["name"] == "Product design"
    assert updated["description"] == "Pixels"
    assert updated["member_count"] == 3
    # A rename to another chat's name is refused.
    make_chat(api, "Taken")
    resp = api.put(f"/api/chats/{chat['id']}", json={**body, "name": "Taken"})
    assert resp.status_code == 409

    assert api.delete(f"/api/chats/{chat['id']}").status_code == 204
    assert api.get(f"/api/chats/{chat['id']}").status_code == 404
    assert api.delete(f"/api/chats/{chat['id']}").status_code == 404
    assert [c["name"] for c in api.get("/api/chats").json()] == ["Taken"]


def test_a_member_removes_a_member(api, user, other):
    chat = make_chat(api, "Design")
    updated = add_user(api, chat, other)
    first, added = updated["members"]["users"]
    # A member of another chat isn't one of this chat's.
    elsewhere = add_user(api, make_chat(api, "Elsewhere"), other)
    stray = elsewhere["members"]["users"][1]["id"]
    assert api.delete(f"/api/chats/{chat['id']}/members/{stray}").status_code == 404

    assert api.delete(f"/api/chats/{chat['id']}/members/{added['id']}").status_code == 204
    assert api.delete(f"/api/chats/{chat['id']}/members/{added['id']}").status_code == 404
    after = api.get(f"/api/chats/{chat['id']}").json()
    assert [m["target_id"] for m in after["members"]["users"]] == [user.id]
    assert after["member_count"] == 2
    # Out of it, the other user still reads it, but can't remove anyone.
    with signed_in_as(other):
        assert api.get(f"/api/chats/{chat['id']}").json()["is_member"] is False
        resp = api.delete(f"/api/chats/{chat['id']}/members/{first['id']}")
        assert resp.status_code == 403


def test_a_chat_of_another_organisation_is_a_404_everywhere(api, user, outsider):
    chat = make_chat(api, "Secret")
    [first] = chat["members"]["users"]
    url = f"/api/chats/{chat['id']}"
    body = {"name": "Secret", "description": ""}
    with signed_in_as(outsider):
        assert api.get(url).status_code == 404
        assert api.put(url, json=body).status_code == 404
        assert api.delete(url).status_code == 404
        assert api.post(f"{url}/messages", json={"body": "Hi"}).status_code == 404
        assert api.delete(f"{url}/members/{first['id']}").status_code == 404
        assert api.get("/api/chats").json() == []


def post(api, chat: dict, body: str) -> dict:
    """Post a message mentioning no one; returns it."""
    resp = api.post(f"/api/chats/{chat['id']}/messages", json={"body": body})
    assert resp.status_code == 201, resp.text
    posted = resp.json()
    assert posted["invocations"] == []
    [message] = posted["messages"]
    return message


def messages(api, chat: dict, **cursor) -> dict:
    resp = api.get(f"/api/chats/{chat['id']}/messages", params=cursor)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_member_posts_a_markdown_message(api, user):
    chat = make_chat(api, "Design")
    message = post(api, chat, "Hello, **world**")
    assert message["chat_id"] == chat["id"]
    assert message["kind"] == "text"
    assert message["body"] == "Hello, **world**"
    assert message["mentions"] == []
    assert message["invocation_id"] is None
    assert message["author"] == {"kind": "user", "id": user.id, "name": "Test User"}
    assert messages(api, chat) == {"messages": [message], "invocations": []}


def test_anyone_in_the_organisation_posts_as_themselves(api, user, other):
    chat = make_chat(api, "Open")
    hello = post(api, chat, "Hello")
    with signed_in_as(other):
        # Read before joining, and posted in, which joins (see test_open_chats.py).
        assert messages(api, chat)["messages"] == [hello]
        theirs = post(api, chat, "Hi")
    assert theirs["author"]["id"] == other.id and theirs["author"]["name"] == "Other Person"
    # An empty message isn't one.
    for body in ("", "  \n"):
        resp = api.post(f"/api/chats/{chat['id']}/messages", json={"body": body})
        assert resp.status_code == 422, repr(body)


def test_posting_makes_the_chat_the_most_recently_active(api):
    first = make_chat(api, "First")
    make_chat(api, "Second")
    assert [c["name"] for c in api.get("/api/chats").json()] == ["Second", "First"]
    post(api, first, "Hello")
    assert [c["name"] for c in api.get("/api/chats").json()] == ["First", "Second"]


def test_the_after_cursor_reads_whats_new_oldest_first(api):
    chat = make_chat(api, "Design")
    assert messages(api, chat) == {"messages": [], "invocations": []}
    one, two, three = (post(api, chat, body) for body in ("one", "two", "three"))
    assert one["id"] < two["id"] < three["id"]
    assert messages(api, chat, after=0)["messages"] == [one, two, three]
    assert messages(api, chat, after=one["id"])["messages"] == [two, three]
    page = messages(api, chat, after=three["id"])
    assert page == {"messages": [], "invocations": []}
    # Another chat's messages aren't this one's.
    elsewhere = post(api, make_chat(api, "Elsewhere"), "elsewhere")
    assert messages(api, chat, after=three["id"])["messages"] == []
    assert elsewhere["id"] > three["id"]


def test_the_before_cursor_pages_back_through_history(api, monkeypatch):
    monkeypatch.setattr(chats, "MESSAGE_PAGE", 2)
    chat = make_chat(api, "Design")
    sent = [post(api, chat, f"message {n}") for n in range(5)]
    # With no cursor, the latest page, oldest first.
    assert messages(api, chat)["messages"] == sent[3:]
    # Then each page before the oldest one shown, until a short page says that's all.
    assert messages(api, chat, before=sent[3]["id"])["messages"] == sent[1:3]
    assert messages(api, chat, before=sent[1]["id"])["messages"] == sent[:1]
    assert messages(api, chat, before=sent[0]["id"])["messages"] == []
    # What's new comes a page at a time too, so a long absence catches up over a few reads.
    assert messages(api, chat, after=0)["messages"] == sent[:2]
    # Both cursors at once is a mistake.
    resp = api.get(f"/api/chats/{chat['id']}/messages", params={"after": 1, "before": 9})
    assert resp.status_code == 422


def test_a_message_outlives_its_author(api, db, user):
    leaver = join_organisation(db, user, "leaver")
    chat = make_chat(api, "Design")
    add_user(api, chat, leaver)
    with signed_in_as(leaver):
        post(api, chat, "Bye")
    db.delete(leaver)
    db.commit()
    [message] = messages(api, chat)["messages"]
    assert message["body"] == "Bye" and message["author"] is None


def make_agent(api, name: str) -> dict:
    resp = api.post("/api/agents", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_workflow(api, name: str) -> dict:
    resp = api.post("/api/workflows", json={"name": name, "nodes": []})
    assert resp.status_code == 201, resp.text
    return resp.json()


def add(api, chat: dict, kind: str, target: dict) -> dict:
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": target["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_member_adds_the_organisations_agents_and_workflows(api, other, outsider):
    chat = make_chat(api, "Design")
    # An agent and a workflow may share a name; they're members of different kinds.
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Review")
    add(api, chat, "agent", agent)
    chat = add(api, chat, "workflow", workflow)
    assert chat["member_count"] == 4
    [assistant, a] = chat["members"]["agents"]
    assert assistant["name"] == "Documents assistant"
    assert (a["kind"], a["target_id"], a["name"]) == ("agent", agent["id"], "Review")
    [w] = chat["members"]["workflows"]
    assert (w["kind"], w["target_id"], w["name"]) == ("workflow", workflow["id"], "Review")
    # Once each.
    for kind, target in (("agent", agent), ("workflow", workflow)):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": target["id"]})
        assert resp.status_code == 409, kind
        assert resp.json()["detail"] == "Already a member of this chat."
    # Only its members add to it.
    with signed_in_as(other):
        member = {"kind": "agent", "id": agent["id"]}
        resp = api.post(f"/api/chats/{chat['id']}/members", json=member)
        assert resp.status_code == 403
    # And only the organisation's agents and workflows, that aren't archived.
    # Ids are each organisation's store's own: one of theirs whose id no agent here has.
    with signed_in_as(outsider):
        for n in range(4):
            theirs = make_agent(api, f"Theirs {n}")
    assert theirs["id"] not in [a["id"] for a in api.get("/api/agents").json()]
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "agent", "id": theirs["id"]})
    assert resp.status_code == 422 and resp.json()["detail"] == "Unknown agent"
    gone = make_workflow(api, "Gone")
    assert api.post(f"/api/workflows/{gone['id']}/archive").status_code == 200
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "workflow", "id": gone["id"]})
    assert resp.status_code == 422 and resp.json()["detail"] == "Unknown workflow"
    assert api.get("/api/chats").json()[0]["member_count"] == 4


def test_the_mentionable_members_are_grouped_by_kind(api, user, other):
    chat = make_chat(api, "Design")
    add_user(api, chat, other)
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Review")
    [assistant] = chat["members"]["agents"]
    add(api, chat, "agent", agent)
    add(api, chat, "workflow", workflow)
    expected = {
        "users": [
            {
                "kind": "user",
                "id": user.id,
                "name": "Test User",
                "first_name": "Test",
                "last_name": "User",
            },
            {
                "kind": "user",
                "id": other.id,
                "name": "Other Person",
                "first_name": "Other",
                "last_name": "Person",
            },
        ],
        "agents": [
            {
                "kind": "agent",
                "id": assistant["target_id"],
                "name": "Documents assistant",
                "first_name": None,
                "last_name": None,
            },
            {
                "kind": "agent",
                "id": agent["id"],
                "name": "Review",
                "first_name": None,
                "last_name": None,
            },
        ],
        "workflows": [
            {
                "kind": "workflow",
                "id": workflow["id"],
                "name": "Review",
                "first_name": None,
                "last_name": None,
            }
        ],
    }
    assert api.get(f"/api/chats/{chat['id']}/mentionable").json() == expected
    # Anyone in the organisation sees who's in it, joined or not.
    with signed_in_as(other):
        assert api.get(f"/api/chats/{chat['id']}/mentionable").json() == expected


def test_an_archived_agent_or_workflow_stays_a_member_shown_archived(api):
    chat = make_chat(api, "Design")
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Digest")
    add(api, chat, "agent", agent)
    add(api, chat, "workflow", workflow)
    assert api.post(f"/api/agents/{agent['id']}/archive").status_code == 200
    assert api.post(f"/api/workflows/{workflow['id']}/archive").status_code == 200
    mentionable = api.get(f"/api/chats/{chat['id']}/mentionable").json()
    assert [a["name"] for a in mentionable["agents"]] == ["Documents assistant"]
    assert mentionable["workflows"] == []
    got = api.get(f"/api/chats/{chat['id']}").json()
    assert [(a["name"], a["archived"]) for a in got["members"]["agents"]] == [
        ("Documents assistant", False),
        ("Review", True),
    ]
    assert [(w["name"], w["archived"]) for w in got["members"]["workflows"]] == [("Digest", True)]
    assert got["member_count"] == 4
    assert api.get("/api/chats").json()[0]["member_count"] == 4


def test_removing_an_agent_or_workflow_leaves_what_it_posted(api, in_store):
    chat = make_chat(api, "Design")
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Digest")
    add(api, chat, "agent", agent)
    chat = add(api, chat, "workflow", workflow)
    # Agents and workflows post only when a mention runs them, which comes later; as if they had.
    in_store.add(
        *[
            ChatMessage(
                chat_id=chat["id"],
                author_kind=ChatAuthorKind.agent,
                author_agent_id=agent["id"],
                kind=ChatMessageKind.text,
                body="From the agent",
            ),
            ChatMessage(
                chat_id=chat["id"],
                author_kind=ChatAuthorKind.workflow,
                author_workflow_id=workflow["id"],
                kind=ChatMessageKind.text,
                body="From the workflow",
            ),
        ]
    )
    for member in chat["members"]["agents"] + chat["members"]["workflows"]:
        resp = api.delete(f"/api/chats/{chat['id']}/members/{member['id']}")
        assert resp.status_code == 204
    after = api.get(f"/api/chats/{chat['id']}").json()
    assert after["members"]["agents"] == [] and after["members"]["workflows"] == []
    posted = messages(api, chat)["messages"]
    assert [(m["body"], m["author"]["kind"], m["author"]["name"]) for m in posted] == [
        ("From the agent", "agent", "Review"),
        ("From the workflow", "workflow", "Digest"),
    ]


def test_mentions_are_stored_structurally(api, user, other):
    chat = make_chat(api, "Design")
    add_user(api, chat, other)
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Review")
    add(api, chat, "agent", agent)
    add(api, chat, "workflow", workflow)
    body = "@[Review](agent:{a}) and @[Review](workflow:{w}), over to @[Other Person](user:{u})"
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={
            "body": body,
            "mentions": [
                {"kind": "agent", "id": agent["id"]},
                {"kind": "workflow", "id": workflow["id"]},
                {"kind": "user", "id": other.id},
                # Twice is once.
                {"kind": "agent", "id": agent["id"]},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    # (The workflow's card follows it.)
    message = resp.json()["messages"][0]
    assert message["mentions"] == [
        {"kind": "agent", "id": agent["id"], "name": "Review"},
        {"kind": "workflow", "id": workflow["id"], "name": "Review"},
        {"kind": "user", "id": other.id, "name": "Other Person"},
    ]
    # The names are as they were when the message was posted.
    resp = api.put(f"/api/agents/{agent['id']}", json={"name": "Critique"})
    assert resp.status_code == 200, resp.text
    assert messages(api, chat)["messages"][0] == message


def test_a_mention_of_a_user_still_points_at_them_once_theyre_renamed(api, db, other):
    chat = make_chat(api, "Design")
    add_user(api, chat, other)
    token = f"@[Other Person](user:{other.id})"
    mention = {"kind": "user", "id": other.id}
    resp = api.post(
        f"/api/chats/{chat['id']}/messages", json={"body": f"Hi {token}", "mentions": [mention]}
    )
    assert resp.status_code == 201, resp.text
    before = resp.json()["messages"][0]

    other.first_name, other.last_name = "Otto", "Persson"
    db.commit()

    # The old message still names them as they were, and still points at them by id.
    assert messages(api, chat)["messages"][0] == before
    assert before["mentions"] == [{"kind": "user", "id": other.id, "name": "Other Person"}]
    # A token with their old name resolves to them by its id, under their new name.
    resp = api.post(
        f"/api/chats/{chat['id']}/messages", json={"body": f"Again {token}", "mentions": [mention]}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["messages"][0]["mentions"] == [
        {"kind": "user", "id": other.id, "name": "Otto Persson"}
    ]
    # Everywhere else shows their new name.
    [_, member] = api.get(f"/api/chats/{chat['id']}").json()["members"]["users"]
    assert member["name"] == "Otto Persson"
    [_, mentionable] = api.get(f"/api/chats/{chat['id']}/mentionable").json()["users"]
    assert mentionable == {
        "kind": "user",
        "id": other.id,
        "name": "Otto Persson",
        "first_name": "Otto",
        "last_name": "Persson",
    }


def test_a_mention_of_anyone_or_anything_the_chat_doesnt_include_is_refused(api, other):
    chat = make_chat(api, "Design")
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Digest")
    add(api, chat, "agent", agent)
    removed = make_workflow(api, "Removed")
    add(api, chat, "workflow", removed)
    member_id = api.get(f"/api/chats/{chat['id']}").json()["members"]["workflows"][0]["id"]
    assert api.delete(f"/api/chats/{chat['id']}/members/{member_id}").status_code == 204
    # A person who hasn't joined.
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": "Hi", "mentions": [{"kind": "user", "id": other.id}]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Only the chat's members can be mentioned in it."
    for mention, name in (
        # Exists, but isn't in the chat.
        ({"kind": "workflow", "id": workflow["id"]}, "Digest"),
        # Was a member, but has since been removed.
        ({"kind": "workflow", "id": removed["id"]}, "Removed"),
    ):
        resp = api.post(
            f"/api/chats/{chat['id']}/messages", json={"body": "Hi", "mentions": [mention]}
        )
        assert resp.status_code == 422, mention
        assert resp.json()["detail"]["missing"] == [{**mention, "name": name}]
    # One that doesn't exist (the agent's id, as a workflow's, with no such workflow).
    missing_id = max(agent["id"], workflow["id"], removed["id"]) + 100
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": "Hi", "mentions": [{"kind": "workflow", "id": missing_id}]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Unknown workflow"
    assert messages(api, chat)["messages"] == []
