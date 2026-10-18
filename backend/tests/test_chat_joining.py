"""Public chats are found in the list, read before joining, joined in one step and left; each chat
the user is in counts the messages they haven't seen."""

import pytest
from test_chat_run_access import signed_up
from test_chats import add_user, make_chat, messages, post, signed_in_as


@pytest.fixture
def other(db):
    """A second user, who finds, joins and leaves the first's chats."""
    yield from signed_up(db, -2, "other")


def listed(api) -> dict[str, dict]:
    """The signed-in user's chat list, by name."""
    resp = api.get("/api/chats")
    assert resp.status_code == 200, resp.text
    return {c["name"]: c for c in resp.json()}


def test_the_list_has_public_chats_im_not_in_flagged_as_not_joined(api, user, other):
    with signed_in_as(other):
        make_chat(api, "Theirs public", visibility="public")
        make_chat(api, "Theirs private", visibility="private")
    make_chat(api, "Mine", visibility="private")
    chats = listed(api)
    # Not their private one: a private chat doesn't exist for anyone not in it.
    assert list(chats) == ["Mine", "Theirs public"]
    theirs = chats["Theirs public"]
    assert (theirs["is_member"], theirs["is_owner"]) == (False, False)
    assert chats["Mine"]["is_member"] is True
    # No unread count for a chat they're not in.
    assert theirs["unread_count"] is None
    assert chats["Mine"]["unread_count"] == 0


def join(api, chat: dict) -> dict:
    resp = api.post(f"/api/chats/{chat['id']}/join")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_public_chat_is_read_before_joining_and_posted_in_after(api, user, other):
    chat = make_chat(api, "Open", visibility="public")
    hello = post(api, chat, "Hello")
    with signed_in_as(other):
        # Readable, but not theirs to post in until they join.
        assert api.get(f"/api/chats/{chat['id']}").json()["is_member"] is False
        assert messages(api, chat)["messages"] == [hello]
        resp = api.post(f"/api/chats/{chat['id']}/messages", json={"body": "Hi"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Only the chat's members can post in it."

        joined = join(api, chat)
        assert joined["is_member"] is True and joined["is_owner"] is False
        assert [(m["target_id"], m["role"]) for m in joined["members"]["users"]] == [
            (user.id, "owner"),
            (other.id, "member"),
        ]
        hi = post(api, chat, "Hi")
        assert listed(api)["Open"]["is_member"] is True
        # Once is enough.
        resp = api.post(f"/api/chats/{chat['id']}/join")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Already a member of this chat."
    assert [m["id"] for m in messages(api, chat)["messages"]] == [hello["id"], hi["id"]]


def test_a_private_chat_cant_be_joined(api, other):
    chat = make_chat(api, "Secret", visibility="private")
    with signed_in_as(other):
        assert api.post(f"/api/chats/{chat['id']}/join").status_code == 404
        assert api.post(f"/api/chats/{chat['id']}/leave").status_code == 404
    # Nor joined again by its owner, who's in it.
    assert api.post(f"/api/chats/{chat['id']}/join").status_code == 409


def test_a_member_leaves_and_rejoins_to_find_the_history_still_there(api, user, other):
    chat = make_chat(api, "Open", visibility="public")
    with signed_in_as(other):
        join(api, chat)
        hi = post(api, chat, "Hi")
        assert api.post(f"/api/chats/{chat['id']}/leave").status_code == 204
        # Out of it, so the chat is back to one they could join, and they can't post.
        left = api.get(f"/api/chats/{chat['id']}").json()
        assert (left["is_member"], left["member_count"]) == (False, 1)
        assert listed(api)["Open"]["is_member"] is False
        assert api.post(f"/api/chats/{chat['id']}/messages", json={"body": "?"}).status_code == 403
        # Leaving twice isn't leaving.
        resp = api.post(f"/api/chats/{chat['id']}/leave")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Not a member of this chat."
        # Nothing went with them.
        assert messages(api, chat)["messages"] == [hi]
        join(api, chat)
        assert messages(api, chat)["messages"] == [hi]


def test_the_owner_cant_leave(api, user):
    chat = make_chat(api, "Mine", visibility="public")
    resp = api.post(f"/api/chats/{chat['id']}/leave")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "The owner can't leave the chat."
    assert api.get(f"/api/chats/{chat['id']}").json()["is_member"] is True


def test_leaving_a_private_chat_makes_it_disappear(api, user, other):
    chat = make_chat(api, "Secret", visibility="private")
    add_user(api, chat, other)
    with signed_in_as(other):
        assert api.post(f"/api/chats/{chat['id']}/leave").status_code == 204
        assert api.get(f"/api/chats/{chat['id']}").status_code == 404
        assert listed(api) == {}


def unread(api, name: str) -> int | None:
    return listed(api)[name]["unread_count"]


def read_up_to(api, chat: dict, message: dict) -> None:
    resp = api.post(f"/api/chats/{chat['id']}/read", json={"message_id": message["id"]})
    assert resp.status_code == 204, resp.text


def test_the_unread_count_rises_with_others_messages_and_clears_on_reading(api, user, other):
    chat = make_chat(api, "Design", visibility="public")
    with signed_in_as(other):
        join(api, chat)
        first = post(api, chat, "one")
        post(api, chat, "two")
        # Their own messages aren't unread for them.
        assert unread(api, "Design") == 0
    assert unread(api, "Design") == 2
    # What one posts oneself doesn't add to it.
    mine = post(api, chat, "mine")
    assert unread(api, "Design") == 2
    read_up_to(api, chat, first)
    assert unread(api, "Design") == 1
    # Read to the bottom: none left.
    read_up_to(api, chat, mine)
    assert unread(api, "Design") == 0
    # Reading an earlier message again (another tab, say) doesn't bring any back.
    read_up_to(api, chat, first)
    assert unread(api, "Design") == 0
    with signed_in_as(other):
        last = post(api, chat, "three")
    assert unread(api, "Design") == 1
    read_up_to(api, chat, last)
    assert unread(api, "Design") == 0


def test_a_member_added_later_has_the_history_to_read(api, user, other):
    chat = make_chat(api, "Design", visibility="private")
    post(api, chat, "one")
    post(api, chat, "two")
    add_user(api, chat, other)
    with signed_in_as(other):
        assert [m["body"] for m in messages(api, chat)["messages"]] == ["one", "two"]
        assert unread(api, "Design") == 2
    # Whereas one who joins has seen what was there to read before they did.
    public = make_chat(api, "Open", visibility="public")
    post(api, public, "hello")
    with signed_in_as(other):
        join(api, public)
        assert unread(api, "Open") == 0


def test_only_a_member_marks_a_chat_read(api, other):
    chat = make_chat(api, "Open", visibility="public")
    hello = post(api, chat, "hello")
    with signed_in_as(other):
        resp = api.post(f"/api/chats/{chat['id']}/read", json={"message_id": hello["id"]})
        assert resp.status_code == 403
    # Another chat's message ids don't read this one ahead of what it has.
    elsewhere = post(api, make_chat(api, "Elsewhere"), "elsewhere")
    with signed_in_as(other):
        join(api, chat)
        read_up_to(api, chat, elsewhere)
    post(api, chat, "after")
    with signed_in_as(other):
        assert unread(api, "Open") == 1
