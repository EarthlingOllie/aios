from contextlib import contextmanager

import pytest

from aios.api import chats
from aios.auth import current_user
from aios.main import app
from aios.models import User


def make_chat(api, name: str, **extra) -> dict:
    resp = api.post("/api/chats", json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def other(db):
    """A second signed-up user, to be another member (or not) of the first's chats."""
    u = User(github_id=-2, login="other", name="Other Person")
    db.add(u)
    db.commit()
    yield u
    db.delete(u)
    db.commit()


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
    assert chat["member_count"] == 1
    assert chat["members"]["agents"] == [] and chat["members"]["workflows"] == []
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
    assert updated["member_count"] == 2
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
        make_chat(api, "Theirs public", visibility="public")
        make_chat(api, "Theirs private", visibility="private")
        add_user(api, theirs_with_me, user)
    listed = api.get("/api/chats").json()
    assert [c["name"] for c in listed] == ["Theirs with me", "Mine"]
    assert [c["visibility"] for c in listed] == ["private", "public"]
    assert [c["is_owner"] for c in listed] == [False, True]
    assert listed[0]["member_count"] == 2
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
    assert updated["member_count"] == 2
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
    assert after["member_count"] == 1
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
        # Not a member, so not listed.
        assert api.get("/api/chats").json() == []


def post(api, chat: dict, body: str) -> dict:
    resp = api.post(f"/api/chats/{chat['id']}/messages", json={"body": body})
    assert resp.status_code == 201, resp.text
    return resp.json()


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
