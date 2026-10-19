from contextlib import contextmanager

import pytest
from conftest import delete_user, sign_up

from aios.api import chats
from aios.auth import current_user
from aios.main import app
from aios.models import ChatAuthorKind, ChatMessage, ChatMessageKind, User


def make_chat(api, name: str, **extra) -> dict:
    resp = api.post("/api/chats", json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def other(db):
    """A second signed-up user, to be another member (or not) of the first's chats."""
    u = sign_up(db, -2, "other", name="Other Person")
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


def test_create_makes_the_creator_the_owner_and_first_member(api, user):
    chat = make_chat(api, "Design", description="Where design happens", visibility="private")
    assert chat["name"] == "Design"
    assert chat["description"] == "Where design happens"
    assert chat["visibility"] == "private"
    assert chat["owner_id"] == user.id
    assert chat["is_owner"] is True
    assert chat["is_member"] is True
    # The owner, and their documents assistant (see test_chat_documents_assistant.py).
    assert chat["member_count"] == 2
    assert [a["name"] for a in chat["members"]["agents"]] == ["Documents assistant"]
    assert chat["members"]["workflows"] == []
    [member] = chat["members"]["users"]
    assert member["kind"] == "user"
    assert member["role"] == "owner"
    assert member["target_id"] == user.id
    assert member["name"] == "test-user"
    assert member["login"] == "test-user"
    assert chat["created_at"] == chat["updated_at"]


def test_names_are_unique_among_chats(api, other):
    make_chat(api, "General")
    resp = api.post("/api/chats", json={"name": "General"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "A chat with that name already exists."
    # Even another user's: a chat is named like a channel, not like something of one's own.
    with signed_in_as(other):
        resp = api.post("/api/chats", json={"name": "General"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "A chat with that name already exists."


def test_read(api):
    chat = make_chat(api, "Design", description="Pixels", visibility="public")
    assert api.get(f"/api/chats/{chat['id']}").json() == chat
    assert api.get(f"/api/chats/{chat['id'] + 1000}").status_code == 404


def add_user(api, chat: dict, user: User) -> dict:
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": user.id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_the_owner_adds_a_user_as_a_member(api, user, other):
    chat = make_chat(api, "Design")
    updated = add_user(api, chat, other)
    assert updated["member_count"] == 3
    owner, member = updated["members"]["users"]
    assert owner["target_id"] == user.id and owner["role"] == "owner"
    assert member["target_id"] == other.id and member["role"] == "member"
    assert member["name"] == "Other Person" and member["login"] == "other"

    # Once only.
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": other.id})
    assert resp.status_code == 409
    # A user who doesn't exist, and kinds that can't be added yet.
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": -1})
    assert resp.status_code == 422
    for kind in ("agent", "workflow"):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": 1})
        assert resp.status_code == 422, kind

    # Only the owner adds members.
    with signed_in_as(other):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": user.id})
        assert resp.status_code == 403


def test_list_is_the_chats_im_in_most_recently_active_first(api, user, other):
    mine = make_chat(api, "Mine", visibility="public")
    with signed_in_as(other):
        theirs_with_me = make_chat(api, "Theirs with me", visibility="private")
        make_chat(api, "Theirs private", visibility="private")
        add_user(api, theirs_with_me, user)
    listed = api.get("/api/chats").json()
    assert [c["name"] for c in listed] == ["Theirs with me", "Mine"]
    assert [c["visibility"] for c in listed] == ["private", "public"]
    assert [c["is_owner"] for c in listed] == [False, True]
    assert listed[0]["member_count"] == 3
    assert "members" not in listed[0]

    # Changing a chat makes it the most recently active.
    resp = api.put(
        f"/api/chats/{mine['id']}",
        json={"name": "Mine", "description": "Now described", "visibility": "public"},
    )
    assert resp.status_code == 200, resp.text
    assert [c["name"] for c in api.get("/api/chats").json()] == ["Mine", "Theirs with me"]


def test_only_the_owner_changes_or_deletes_a_chat(api, user, other):
    chat = make_chat(api, "Design", visibility="private")
    add_user(api, chat, other)
    body = {"name": "Product design", "description": "Pixels", "visibility": "public"}
    with signed_in_as(other):
        # A member sees it, but can't change or delete it.
        assert api.get(f"/api/chats/{chat['id']}").status_code == 200
        resp = api.put(f"/api/chats/{chat['id']}", json=body)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Only the chat's owner can do that."
        assert api.delete(f"/api/chats/{chat['id']}").status_code == 403
    unchanged = api.get(f"/api/chats/{chat['id']}").json()
    assert unchanged["name"] == "Design" and unchanged["visibility"] == "private"

    updated = api.put(f"/api/chats/{chat['id']}", json=body).json()
    assert updated["name"] == "Product design"
    assert updated["description"] == "Pixels"
    assert updated["visibility"] == "public"
    assert updated["member_count"] == 3
    # A rename to another chat's name is refused.
    make_chat(api, "Taken")
    resp = api.put(f"/api/chats/{chat['id']}", json={**body, "name": "Taken"})
    assert resp.status_code == 409

    assert api.delete(f"/api/chats/{chat['id']}").status_code == 204
    assert api.get(f"/api/chats/{chat['id']}").status_code == 404
    assert api.delete(f"/api/chats/{chat['id']}").status_code == 404
    assert [c["name"] for c in api.get("/api/chats").json()] == ["Taken"]
    with signed_in_as(other):
        assert api.get("/api/chats").json() == []


def test_only_the_owner_removes_a_member(api, user, other):
    chat = make_chat(api, "Design")
    updated = add_user(api, chat, other)
    owner, member = updated["members"]["users"]
    with signed_in_as(other):
        resp = api.delete(f"/api/chats/{chat['id']}/members/{owner['id']}")
        assert resp.status_code == 403
        assert api.delete(f"/api/chats/{chat['id']}/members/{member['id']}").status_code == 403
    # The owner stays: a chat always has one.
    resp = api.delete(f"/api/chats/{chat['id']}/members/{owner['id']}")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "The owner can't be removed from the chat."
    # A member of another chat isn't one of this chat's.
    elsewhere = add_user(api, make_chat(api, "Elsewhere"), other)
    stray = elsewhere["members"]["users"][1]["id"]
    assert api.delete(f"/api/chats/{chat['id']}/members/{stray}").status_code == 404

    assert api.delete(f"/api/chats/{chat['id']}/members/{member['id']}").status_code == 204
    assert api.delete(f"/api/chats/{chat['id']}/members/{member['id']}").status_code == 404
    after = api.get(f"/api/chats/{chat['id']}").json()
    assert [m["target_id"] for m in after["members"]["users"]] == [user.id]
    assert after["member_count"] == 2
    # Removed from a private chat, the other user no longer sees it.
    with signed_in_as(other):
        assert api.get(f"/api/chats/{chat['id']}").status_code == 404
        assert [c["name"] for c in api.get("/api/chats").json()] == ["Elsewhere"]


def test_a_private_chat_im_not_in_is_a_404_everywhere(api, user, other):
    chat = make_chat(api, "Secret", visibility="private")
    [owner] = chat["members"]["users"]
    url = f"/api/chats/{chat['id']}"
    body = {"name": "Secret", "description": "", "visibility": "public"}
    with signed_in_as(other):
        assert api.get(url).status_code == 404
        assert api.put(url, json=body).status_code == 404
        assert api.delete(url).status_code == 404
        assert api.post(f"{url}/members", json={"kind": "user", "id": other.id}).status_code == 404
        assert api.delete(f"{url}/members/{owner['id']}").status_code == 404
        assert api.get("/api/chats").json() == []

    # A public chat exists for everyone: it can be read, but is still the owner's to change.
    public = make_chat(api, "Open", visibility="public")
    with signed_in_as(other):
        assert api.get(f"/api/chats/{public['id']}").json()["is_owner"] is False
        assert api.put(f"/api/chats/{public['id']}", json=body).status_code == 403
        assert api.delete(f"/api/chats/{public['id']}").status_code == 403
        # Listed for them to find, as one they're not in.
        assert [(c["name"], c["is_member"]) for c in api.get("/api/chats").json()] == [
            ("Open", False)
        ]


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
    assert message["author"] == {
        "kind": "user",
        "id": user.id,
        "name": "test-user",
        "login": "test-user",
        "avatar_url": None,
    }
    assert messages(api, chat) == {"messages": [message], "invocations": []}


def test_only_members_post(api, user, other):
    public = make_chat(api, "Open", visibility="public")
    private = make_chat(api, "Secret", visibility="private")
    hello = post(api, public, "Hello")
    with signed_in_as(other):
        # A public chat can be read, but not posted in, by someone who isn't in it.
        assert api.get(f"/api/chats/{public['id']}").json()["is_member"] is False
        assert messages(api, public)["messages"] == [hello]
        resp = api.post(f"/api/chats/{public['id']}/messages", json={"body": "Hi"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Only the chat's members can post in it."
        # A private one doesn't exist for them.
        resp = api.post(f"/api/chats/{private['id']}/messages", json={"body": "Hi"})
        assert resp.status_code == 404
        assert api.get(f"/api/chats/{private['id']}/messages").status_code == 404
    assert messages(api, public)["messages"] == [hello]

    # Once added, they can post, as themselves.
    add_user(api, private, other)
    with signed_in_as(other):
        theirs = post(api, private, "Hi")
    assert theirs["author"]["id"] == other.id and theirs["author"]["name"] == "Other Person"
    # An empty message isn't one.
    for body in ("", "  \n"):
        resp = api.post(f"/api/chats/{private['id']}/messages", json={"body": body})
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


def test_a_message_outlives_its_author(api, db):
    leaver = User(github_id=-3, login="leaver")
    db.add(leaver)
    db.commit()
    chat = make_chat(api, "Design", visibility="public")
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


def test_the_owner_adds_their_agents_and_workflows(api, other):
    chat = make_chat(api, "Design", visibility="public")
    # An agent and a workflow may share a name; they're members of different kinds.
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Review")
    add(api, chat, "agent", agent)
    chat = add(api, chat, "workflow", workflow)
    assert chat["member_count"] == 4
    [assistant, a] = chat["members"]["agents"]
    assert assistant["name"] == "Documents assistant"
    assert (a["kind"], a["target_id"], a["name"]) == ("agent", agent["id"], "Review")
    assert a["role"] is None
    [w] = chat["members"]["workflows"]
    assert (w["kind"], w["target_id"], w["name"], w["role"]) == (
        "workflow",
        workflow["id"],
        "Review",
        None,
    )
    # Once each.
    for kind, target in (("agent", agent), ("workflow", workflow)):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": target["id"]})
        assert resp.status_code == 409, kind
        assert resp.json()["detail"] == "Already a member of this chat."
    # Only the owner adds members.
    with signed_in_as(other):
        theirs = make_agent(api, "Theirs")
        member = {"kind": "agent", "id": agent["id"]}
        resp = api.post(f"/api/chats/{chat['id']}/members", json=member)
        assert resp.status_code == 403
    # And only agents and workflows they own, that haven't been deleted.
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "agent", "id": theirs["id"]})
    assert resp.status_code == 422 and resp.json()["detail"] == "Unknown agent"
    gone = make_workflow(api, "Gone")
    assert api.delete(f"/api/workflows/{gone['id']}").status_code == 204
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "workflow", "id": gone["id"]})
    assert resp.status_code == 422 and resp.json()["detail"] == "Unknown workflow"
    assert api.get("/api/chats").json()[0]["member_count"] == 4


def test_the_mentionable_members_are_grouped_by_kind(api, user, other):
    chat = make_chat(api, "Design", visibility="public")
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
                "name": "test-user",
                "login": "test-user",
                "avatar_url": None,
            },
            {
                "kind": "user",
                "id": other.id,
                "name": "Other Person",
                "login": "other",
                "avatar_url": None,
            },
        ],
        "agents": [
            {
                "kind": "agent",
                "id": assistant["target_id"],
                "name": "Documents assistant",
                "login": None,
                "avatar_url": None,
            },
            {
                "kind": "agent",
                "id": agent["id"],
                "name": "Review",
                "login": None,
                "avatar_url": None,
            },
        ],
        "workflows": [
            {
                "kind": "workflow",
                "id": workflow["id"],
                "name": "Review",
                "login": None,
                "avatar_url": None,
            }
        ],
    }
    assert api.get(f"/api/chats/{chat['id']}/mentionable").json() == expected
    # Anyone the chat exists for sees who's in it; a private chat doesn't exist for outsiders.
    private = make_chat(api, "Secret")
    with signed_in_as(other):
        assert api.get(f"/api/chats/{chat['id']}/mentionable").json() == expected
        assert api.get(f"/api/chats/{private['id']}/mentionable").status_code == 404


def test_a_deleted_agent_or_workflow_is_no_longer_a_member(api):
    chat = make_chat(api, "Design")
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Digest")
    add(api, chat, "agent", agent)
    add(api, chat, "workflow", workflow)
    assert api.delete(f"/api/agents/{agent['id']}").status_code == 204
    assert api.delete(f"/api/workflows/{workflow['id']}").status_code == 204
    # Only the documents assistant is left of its agents.
    mentionable = api.get(f"/api/chats/{chat['id']}/mentionable").json()
    assert [a["name"] for a in mentionable["agents"]] == ["Documents assistant"]
    assert mentionable["workflows"] == []
    got = api.get(f"/api/chats/{chat['id']}").json()
    assert [a["name"] for a in got["members"]["agents"]] == ["Documents assistant"]
    assert got["members"]["workflows"] == []
    assert got["member_count"] == 2
    assert api.get("/api/chats").json()[0]["member_count"] == 2


def test_removing_an_agent_or_workflow_leaves_what_it_posted(api, db):
    chat = make_chat(api, "Design")
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Digest")
    add(api, chat, "agent", agent)
    chat = add(api, chat, "workflow", workflow)
    # Agents and workflows post only when a mention runs them, which comes later; as if they had.
    db.add_all(
        [
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
    db.commit()
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


def test_a_mention_of_a_non_member_is_refused(api, other):
    chat = make_chat(api, "Design")
    agent = make_agent(api, "Review")
    workflow = make_workflow(api, "Digest")
    add(api, chat, "agent", agent)
    gone = make_agent(api, "Gone")
    add(api, chat, "agent", gone)
    assert api.delete(f"/api/agents/{gone['id']}").status_code == 204
    for mention in (
        # Exists, but isn't in the chat.
        {"kind": "workflow", "id": workflow["id"]},
        {"kind": "user", "id": other.id},
        # The agent's id, as another kind.
        {"kind": "workflow", "id": agent["id"]},
        # Was a member, but has since been deleted.
        {"kind": "agent", "id": gone["id"]},
    ):
        resp = api.post(
            f"/api/chats/{chat['id']}/messages", json={"body": "Hi", "mentions": [mention]}
        )
        assert resp.status_code == 422, mention
        assert resp.json()["detail"] == "Only the chat's members can be mentioned in it."
    assert messages(api, chat)["messages"] == []
