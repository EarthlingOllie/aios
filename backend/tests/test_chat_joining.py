"""Every chat is found in the list, read before joining, joined in one step and left; each chat
the user is in counts the messages they haven't seen."""

import pytest
from conftest import delete_user, join_organisation
from test_chats import make_chat, messages, post, signed_in_as


@pytest.fixture
def other(db, user):
    """A second member of the first user's organisation, who finds, joins and leaves their
    chats."""
    u = join_organisation(db, user, "other")
    yield u
    delete_user(db, u)


def listed(api) -> dict[str, dict]:
    """The signed-in user's chat list, by name."""
    resp = api.get("/api/chats")
    assert resp.status_code == 200, resp.text
    return {c["name"]: c for c in resp.json()}


def test_the_list_has_chats_im_not_in_flagged_as_not_joined(api, user, other):
    with signed_in_as(other):
        make_chat(api, "Theirs")
    make_chat(api, "Mine")
    chats = listed(api)
    assert list(chats) == ["Mine", "Theirs"]
    theirs = chats["Theirs"]
    assert theirs["is_member"] is False
    assert chats["Mine"]["is_member"] is True
    # No unread count for a chat they're not in.
    assert theirs["unread_count"] is None
    assert chats["Mine"]["unread_count"] == 0


def join(api, chat: dict) -> dict:
    resp = api.post(f"/api/chats/{chat['id']}/join")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_chat_is_read_before_joining_and_joined_in_one_step(api, user, other):
    chat = make_chat(api, "Open")
    hello = post(api, chat, "Hello")
    with signed_in_as(other):
        assert api.get(f"/api/chats/{chat['id']}").json()["is_member"] is False
        assert messages(api, chat)["messages"] == [hello]

        joined = join(api, chat)
        assert joined["is_member"] is True
        assert [m["target_id"] for m in joined["members"]["users"]] == [user.id, other.id]
        hi = post(api, chat, "Hi")
        assert listed(api)["Open"]["is_member"] is True
        # Once is enough.
        resp = api.post(f"/api/chats/{chat['id']}/join")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Already a member of this chat."
    assert [m["id"] for m in messages(api, chat)["messages"]] == [hello["id"], hi["id"]]


def test_a_member_leaves_and_rejoins_to_find_the_history_still_there(api, user, other):
    chat = make_chat(api, "Open")
    with signed_in_as(other):
        join(api, chat)
        hi = post(api, chat, "Hi")
        assert api.post(f"/api/chats/{chat['id']}/leave").status_code == 204
        # Out of it, so the chat is back to one they could join.
        left = api.get(f"/api/chats/{chat['id']}").json()
        # Its maker and their documents assistant.
        assert (left["is_member"], left["member_count"]) == (False, 2)
        assert listed(api)["Open"]["is_member"] is False
        # Leaving twice isn't leaving.
        resp = api.post(f"/api/chats/{chat['id']}/leave")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Not a member of this chat."
        # Nothing went with them.
        assert messages(api, chat)["messages"] == [hi]
        join(api, chat)
        assert messages(api, chat)["messages"] == [hi]


def test_the_last_person_leaves_and_the_chat_stays(api, user, other):
    chat = make_chat(api, "Mine")
    hello = post(api, chat, "Hello")
    assert api.post(f"/api/chats/{chat['id']}/leave").status_code == 204
    left = api.get(f"/api/chats/{chat['id']}").json()
    assert left["is_member"] is False and left["members"]["users"] == []
    assert listed(api)["Mine"]["is_member"] is False
    # Still there for anyone to read and join.
    with signed_in_as(other):
        assert messages(api, chat)["messages"] == [hello]
        assert join(api, chat)["is_member"] is True


def unread(api, name: str) -> int | None:
    return listed(api)[name]["unread_count"]


def read_up_to(api, chat: dict, message: dict) -> None:
    resp = api.post(f"/api/chats/{chat['id']}/read", json={"message_id": message["id"]})
    assert resp.status_code == 204, resp.text


def test_the_unread_count_rises_with_others_messages_and_clears_on_reading(api, user, other):
    chat = make_chat(api, "Design")
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


def test_one_who_joins_later_reads_the_history_and_has_seen_it(api, user, other):
    """Nobody adds a person: they join, and what was there to read before counts as seen."""
    chat = make_chat(api, "Design")
    post(api, chat, "one")
    post(api, chat, "two")
    with signed_in_as(other):
        join(api, chat)
        assert [m["body"] for m in messages(api, chat)["messages"]] == ["one", "two"]
        assert unread(api, "Design") == 0
    post(api, chat, "three")
    with signed_in_as(other):
        assert unread(api, "Design") == 1


def test_only_a_member_marks_a_chat_read(api, other):
    chat = make_chat(api, "Open")
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
