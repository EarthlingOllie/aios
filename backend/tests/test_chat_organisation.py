"""Chats belong to the organisation: "public" is every member of it, the owner adds any of its
agents, workflows and members, names are unique within it, each new chat gets its documents
assistant, and to anyone in another organisation its chats don't exist."""

import threading
import time
import warnings
from contextlib import contextmanager

from conftest import delete_user, home_organisation, join_organisation, signed_in_store
from sqlalchemy import func, select, text
from test_chats import add, add_user, make_agent, make_chat, make_workflow, post, signed_in_as
from test_organisation_scoping import member, outsider, person  # noqa: F401
from test_organisations import in_the_background

from aios import organisations
from aios.api import chats
from aios.datastores import store_session
from aios.datastores.models import Chat, ChatMember, ChatMessage, Invocation
from aios.db import SessionLocal, engine
from aios.models import (
    ChatMemberKind,
    User,
)

NAME_TAKEN = "The organisation already has a chat with that name."


def every_endpoint(api, chat: dict, user_id: int, member_id: int = 1) -> dict[str, int]:
    """The status of each of the chat's endpoints for the signed-in user, adding the user
    `user_id` and removing the member row `member_id`."""
    url = f"/api/chats/{chat['id']}"
    body = {"name": "Mine now", "description": "", "visibility": "public"}
    responses = {
        "read": api.get(url),
        "messages": api.get(f"{url}/messages"),
        "mentionable": api.get(f"{url}/mentionable"),
        "join": api.post(f"{url}/join"),
        "leave": api.post(f"{url}/leave"),
        "post": api.post(f"{url}/messages", json={"body": "Hi"}),
        "mark read": api.post(f"{url}/read", json={"message_id": 1}),
        "change": api.put(url, json=body),
        "add member": api.post(f"{url}/members", json={"kind": "user", "id": user_id}),
        "remove member": api.delete(f"{url}/members/{member_id}"),
        "delete": api.delete(url),
    }
    return {what: r.status_code for what, r in responses.items()}


def is_gone(db, chat: dict) -> bool:
    """Whether the chat is gone from its store, and its members, messages and invocations."""
    with store_session(signed_in_store()) as store:
        left = [
            store.scalar(select(func.count()).where(model.chat_id == chat["id"]))
            for model in (ChatMember, ChatMessage, Invocation)
        ]
        return store.get(Chat, chat["id"]) is None and left == [0, 0, 0]


def test_another_organisation_cant_reach_a_chat_public_or_private(api, user, outsider):  # noqa: F811
    public = make_chat(api, "Open", visibility="public")
    private = make_chat(api, "Secret", visibility="private")
    for chat in (public, private):
        post(api, chat, "Hello")
    [owner] = public["members"]["users"]
    with signed_in_as(outsider):
        assert api.get("/api/chats").json() == []
        for chat in (public, private):
            got = every_endpoint(api, chat, outsider.id, owner["id"])
            assert got == {what: 404 for what in got}, chat["name"]
    # Untouched.
    assert [c["name"] for c in api.get("/api/chats").json()] == ["Secret", "Open"]


def test_public_means_every_member_of_the_organisation(api, user, member):  # noqa: F811
    chat = make_chat(api, "Open", visibility="public")
    make_chat(api, "Secret", visibility="private")
    with signed_in_as(member):
        listed = api.get("/api/chats").json()
        assert [(c["name"], c["is_member"]) for c in listed] == [("Open", False)]
        resp = api.post(f"/api/chats/{chat['id']}/join")
        assert resp.status_code == 200, resp.text
        post(api, chat, "Hi")


def test_chat_names_are_unique_within_the_organisation(api, member, outsider):  # noqa: F811
    make_chat(api, "general")
    # Another member's "general" is the same name in the same organisation.
    with signed_in_as(member):
        resp = api.post("/api/chats", json={"name": "general"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == NAME_TAKEN
        mine = make_chat(api, "random")
        body = {"name": "general", "description": "", "visibility": "private"}
        resp = api.put(f"/api/chats/{mine['id']}", json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"] == NAME_TAKEN
    # Another organisation has a "general" of its own.
    with signed_in_as(outsider):
        theirs = make_chat(api, "general")
        assert [c["id"] for c in api.get("/api/chats").json()] == [theirs["id"]]


def test_the_owner_adds_another_members_agent_and_workflow_and_a_fellow_member(
    api,
    user,
    member,  # noqa: F811
    outsider,  # noqa: F811
):
    chat = make_chat(api, "Design")
    with signed_in_as(member):
        agent = make_agent(api, "Their reviewer")
        workflow = make_workflow(api, "Their digest")
    add(api, chat, "agent", agent)
    add(api, chat, "workflow", workflow)
    chat = add_user(api, chat, member)
    assert [m["name"] for m in chat["members"]["agents"]] == [
        "Documents assistant",
        "Their reviewer",
    ]
    assert [m["name"] for m in chat["members"]["workflows"]] == ["Their digest"]
    assert [m["target_id"] for m in chat["members"]["users"]] == [user.id, member.id]
    mentionable = api.get(f"/api/chats/{chat['id']}/mentionable").json()
    assert [m["id"] for m in mentionable["users"]] == [user.id, member.id]
    # A fellow member is theirs to mention.
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": "Over to you", "mentions": [{"kind": "user", "id": member.id}]},
    )
    assert resp.status_code == 201, resp.text

    # Nothing of another organisation's, and nobody from it: they don't exist here.
    # Ids are each organisation's store's own: ones of theirs whose ids nothing here has.
    with signed_in_as(outsider):
        for n in range(4):
            theirs = make_agent(api, f"Outsider's agent {n}")
            their_workflow = make_workflow(api, f"Outsider's workflow {n}")
    assert theirs["id"] not in [a["id"] for a in api.get("/api/agents").json()]
    assert their_workflow["id"] not in [w["id"] for w in api.get("/api/workflows").json()]
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": outsider.id})
    assert resp.status_code == 404
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": -999})
    assert resp.status_code == 404
    for kind, target in (("agent", theirs), ("workflow", their_workflow)):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": target["id"]})
        assert resp.status_code == 422, kind
        assert resp.json()["detail"] == f"Unknown {kind}"
    assert api.get(f"/api/chats/{chat['id']}").json()["member_count"] == 5


def test_a_new_chats_first_agent_is_the_organisations_documents_assistant(
    api,
    member,  # noqa: F811
    outsider,  # noqa: F811
):
    mine = make_chat(api, "Mine")
    with signed_in_as(member):
        theirs = make_chat(api, "Theirs")
    with signed_in_as(outsider):
        elsewhere = make_chat(api, "Elsewhere")
    [assistant] = mine["members"]["agents"]
    assert assistant["name"] == "Documents assistant"
    # The same agent for every member's chat, whoever made it.
    assert [a["target_id"] for a in theirs["members"]["agents"]] == [assistant["target_id"]]
    # Another organisation's is its own, in its store (whatever its id there).
    [their_assistant] = elsewhere["members"]["agents"]
    with signed_in_as(outsider):
        made = api.get(f"/api/agents/{their_assistant['target_id']}").json()
    assert made["created_by"]["id"] == outsider.id
    mine_made = api.get(f"/api/agents/{assistant['target_id']}").json()
    assert mine_made["created_by"]["id"] != outsider.id


def test_a_chat_says_who_made_it_and_who_last_changed_it(api, user, member):  # noqa: F811
    chat = make_chat(api, "Design", visibility="public")
    assert (chat["created_by"], chat["updated_by"]) == (person(user), person(user))
    with signed_in_as(member):
        [listed] = api.get("/api/chats").json()
        assert (listed["created_by"], listed["updated_by"]) == (person(user), person(user))
        body = {"name": "Design", "description": "Pixels", "visibility": "public"}
        assert api.put(f"/api/chats/{chat['id']}", json=body).status_code == 403
    body = {"name": "Product design", "description": "", "visibility": "public"}
    changed = api.put(f"/api/chats/{chat['id']}", json=body).json()
    assert (changed["created_by"], changed["updated_by"]) == (person(user), person(user))
    assert api.get(f"/api/chats/{chat['id']}").json()["created_by"] == person(user)


def owners(api, chat: dict) -> list[int]:
    """The users who hold the chat's owner role, as its members list says."""
    users = api.get(f"/api/chats/{chat['id']}").json()["members"]["users"]
    return [m["target_id"] for m in users if m["role"] == "owner"]


def test_a_chat_owner_who_leaves_the_organisation_hands_it_to_the_member_added_first(
    api,
    db,
    user,
    member,  # noqa: F811
):
    third = join_organisation(db, user, "third")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Handover", visibility="private")
            add_user(api, chat, user)
            add_user(api, chat, third)
            assert api.post("/api/organisation/leave").status_code == 200

        # The chat stays the organisation's, with the member who was added first as its owner.
        got = api.get(f"/api/chats/{chat['id']}").json()
        assert got["is_owner"] is True
        assert owners(api, chat) == [user.id]
        body = {"name": "Ours now", "description": "", "visibility": "private"}
        assert api.put(f"/api/chats/{chat['id']}", json=body).status_code == 200
        with signed_in_as(third):
            assert api.get(f"/api/chats/{chat['id']}").json()["is_owner"] is False
            assert api.put(f"/api/chats/{chat['id']}", json=body).status_code == 403
    finally:
        delete_user(db, third)


def test_a_chat_its_owner_left_with_nobody_in_it_goes_to_whoever_joins_it(
    api,
    user,
    member,  # noqa: F811
):
    with signed_in_as(member):
        chat = make_chat(api, "Abandoned", visibility="public")
        assert api.post("/api/organisation/leave").status_code == 200

    [listed] = api.get("/api/chats").json()
    assert (listed["is_member"], listed["is_owner"]) == (False, False)
    joined = api.post(f"/api/chats/{chat['id']}/join").json()
    assert joined["is_owner"] is True
    assert owners(api, chat) == [user.id]
    assert api.delete(f"/api/chats/{chat['id']}").status_code == 204


def test_a_private_chat_its_owner_left_with_nobody_in_it_is_deleted(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Private, with nobody in it, it would exist for nobody: nobody could join it, so nobody could
    own, change or delete it, and its name would stay taken. It goes, as its owner deleting it
    would: its messages and members with it, and its name free."""
    with signed_in_as(member):
        chat = make_chat(api, "Secrets", visibility="private")
        post(api, chat, "Notes for the team")
        assert api.post("/api/organisation/leave").status_code == 200

    assert api.get("/api/chats").json() == []
    got = every_endpoint(api, chat, user.id)
    assert got == {what: 404 for what in got}
    assert is_gone(db, chat)
    assert api.post("/api/chats", json={"name": "Secrets"}).status_code == 201


def test_a_private_chat_with_another_user_in_it_stays_private_when_its_owner_leaves(
    api,
    user,
    member,  # noqa: F811
):
    with signed_in_as(member):
        chat = make_chat(api, "Two of us", visibility="private")
        add_user(api, chat, user)
        assert api.post("/api/organisation/leave").status_code == 200

    got = api.get(f"/api/chats/{chat['id']}").json()
    assert (got["visibility"], got["is_owner"]) == ("private", True)


# Another backend waiting on a lock: a request waiting on the transaction a test holds open, in
# the platform database or in one of its organisations' stores. From `pg_locks`, which shows
# every backend's (`pg_stat_activity` hides what other roles' backends, the stores', wait on).
WAITING_ON_A_LOCK = text(
    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
    " WHERE NOT l.granted AND l.pid <> pg_backend_pid()"
    " AND (a.datname = current_database() OR a.datname IN (SELECT dbname FROM data_stores))"
)


def lock_waits(conn) -> int:
    """How many backends wait on a lock in the platform database or one of its stores."""
    return conn.execute(WAITING_ON_A_LOCK).scalar()


def waits_on_a_lock(request: threading.Thread) -> None:
    """Return once the request on the thread is waiting on a lock, failing if it finishes
    instead: what it does next can only happen after the transaction it waits on ends."""
    deadline = time.monotonic() + 10
    while True:
        # A transaction of its own each time: one sees the activity as it was when it began.
        with engine.connect() as conn:
            if lock_waits(conn):
                return
        assert request.is_alive(), "the request should wait on the transaction held open"
        assert time.monotonic() < deadline, "the request never waited on a lock"
        time.sleep(0.02)


def finishes_without_waiting(request: threading.Thread) -> None:
    """Return once the request on the thread has finished, failing if it waits on a lock first:
    it isn't held up by the transaction a test holds open."""
    deadline = time.monotonic() + 10
    while request.is_alive():
        with engine.connect() as conn:
            assert not lock_waits(conn), "the request waited on a lock"
        assert time.monotonic() < deadline, "the request never finished"
        time.sleep(0.02)


@contextmanager
def leaving(leaver: User):
    """The user leaving the organisation (as `POST /api/organisation/leave` does), held open:
    flushed within the block (their chat member rows deleted, `hand_over_chats` run, the chats they
    owned locked) and committed as it ends."""
    with SessionLocal() as session:
        user = session.get_one(User, leaver.id)
        org = home_organisation(session, user.id)
        assert organisations.leave_refusal(session, org, user) is None
        organisations.leave(session, org, user)
        session.flush()
        yield
        session.commit()


def test_joining_as_the_owner_leaves_the_organisation_makes_the_joiner_its_owner(
    api,
    user,
    member,  # noqa: F811
):
    """The join reads the chat's members (the owner among them) before the leave deleted the
    owner's row, and the leave commits first: it found nobody in the chat to hand it to, so the
    joiner is the one to own it."""
    with signed_in_as(member):
        chat = make_chat(api, "Busy", visibility="public")
    with leaving(member):
        joining, joined = in_the_background(lambda: api.post(f"/api/chats/{chat['id']}/join"))
        waits_on_a_lock(joining)
    joining.join(timeout=10)

    assert "error" not in joined, joined
    assert joined["value"].status_code == 200, joined["value"].text
    assert joined["value"].json()["is_owner"] is True
    assert owners(api, chat) == [user.id]


def test_leaving_as_someone_joins_hands_the_chat_to_the_joiner(
    api,
    user,
    member,  # noqa: F811
    monkeypatch,
):
    """The other order: the join is in first, so it joins as a member of a chat that has its
    owner, and the leave, waiting for it, finds the joiner in the chat to hand it to."""
    with signed_in_as(member):
        chat = make_chat(api, "Busy", visibility="public")
    # The join, held between deciding the role it joins with and adding its member row.
    reached, release = threading.Event(), threading.Event()
    newest = chats.newest_message_id

    def held(db, chat_id):
        reached.set()
        assert release.wait(timeout=10)
        return newest(db, chat_id)

    monkeypatch.setattr(chats, "newest_message_id", held)
    try:
        joining, joined = in_the_background(lambda: api.post(f"/api/chats/{chat['id']}/join"))
        assert reached.wait(timeout=10)
        with signed_in_as(member):
            leaving_it, left = in_the_background(lambda: api.post("/api/organisation/leave"))
            waits_on_a_lock(leaving_it)
    finally:
        release.set()
    joining.join(timeout=10)
    leaving_it.join(timeout=10)

    assert "error" not in joined, joined
    assert (joined["value"].status_code, joined["value"].json()["is_owner"]) == (200, False)
    assert "error" not in left, left
    assert left["value"].status_code == 200, left["value"].text
    assert owners(api, chat) == [user.id]


def test_an_owner_who_has_left_the_organisation_cant_add_a_member_to_the_chat_as_they_go(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """The add read the leaver as the chat's owner before their leave deleted the row, and the
    leave commits first, having found nobody in the chat to hand it to: a private chat, so it
    deleted it. The add is a 404, as for a chat deleted any other way, not a member added to a
    chat that's gone."""
    with signed_in_as(member):
        chat = make_chat(api, "Ours", visibility="private")
        with leaving(member):
            adding, added = in_the_background(
                lambda: api.post(
                    f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": user.id}
                )
            )
            waits_on_a_lock(adding)
    adding.join(timeout=10)

    assert "error" not in added, added
    assert added["value"].status_code == 404, added["value"].text
    assert is_gone(db, chat)


def test_an_owner_who_has_left_the_organisation_cant_add_a_member_to_a_public_chat_as_they_go(
    api,
    user,
    member,  # noqa: F811
):
    """The same with a public chat: the leave keeps it, with nobody to own it, and the add, which
    read the leaver as its owner, is refused. Whoever joins it owns it."""
    with signed_in_as(member):
        chat = make_chat(api, "Ours", visibility="public")
        with leaving(member):
            adding, added = in_the_background(
                lambda: api.post(
                    f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": user.id}
                )
            )
            waits_on_a_lock(adding)
    adding.join(timeout=10)

    assert "error" not in added, added
    assert added["value"].status_code == 403, added["value"].text
    assert api.post(f"/api/chats/{chat['id']}/join").json()["is_owner"] is True


def test_an_owner_who_has_left_the_organisation_cant_remove_the_member_it_was_handed_to(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """The removal read the leaver as the chat's owner before their leave deleted the row, and the
    leave commits first, having handed the chat to the member added first: removing that member
    then would leave the others in a chat nobody owns."""
    third = join_organisation(db, user, "third")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Ours", visibility="private")
            first = next(
                m
                for m in add_user(api, chat, user)["members"]["users"]
                if m["target_id"] == user.id
            )
            add_user(api, chat, third)
            with leaving(member):
                removing, removed = in_the_background(
                    lambda: api.delete(f"/api/chats/{chat['id']}/members/{first['id']}")
                )
                waits_on_a_lock(removing)
        removing.join(timeout=10)

        assert "error" not in removed, removed
        assert removed["value"].status_code == 403, removed["value"].text
        assert owners(api, chat) == [user.id]
    finally:
        delete_user(db, third)


def test_a_member_cant_leave_a_chat_just_handed_to_them(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Leaving the chat read the user as a member of it before the owner's leaving the
    organisation handed it to them, and the hand-over commits first: their leaving it then would
    leave the others in a chat nobody owns."""
    third = join_organisation(db, user, "third")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Ours", visibility="private")
            add_user(api, chat, user)
            add_user(api, chat, third)
        with leaving(member):
            quitting, quit = in_the_background(lambda: api.post(f"/api/chats/{chat['id']}/leave"))
            waits_on_a_lock(quitting)
        quitting.join(timeout=10)

        assert "error" not in quit, quit
        assert quit["value"].status_code == 422, quit["value"].text
        assert quit["value"].json()["detail"] == "The owner can't leave the chat."
        assert owners(api, chat) == [user.id]
    finally:
        delete_user(db, third)


def users_in(db, chat: dict) -> list[int]:
    """The chat's user members, in the order they were added, whoever can see it."""
    with store_session(signed_in_store()) as store:
        return list(
            store.scalars(
                select(ChatMember.user_id)
                .where(ChatMember.chat_id == chat["id"], ChatMember.kind == ChatMemberKind.user)
                .order_by(ChatMember.id)
            )
        )


def test_a_member_added_to_a_chat_as_they_leave_the_organisation_isnt_added(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """The owner adds Carol while her leave is under way: the add saw her a member, and her leave
    has already taken her out of its chats. Had it committed her row, she'd be in a chat of an
    organisation she isn't in, and the member added first, so its owner when its owner leaves:
    an owner it doesn't exist for, and nobody left in the organisation who could manage it."""
    carol = join_organisation(db, user, "carol")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Plans", visibility="private")
            with leaving(carol):
                adding, result = in_the_background(
                    lambda: api.post(
                        f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": carol.id}
                    )
                )
                waits_on_a_lock(adding)
            adding.join(timeout=10)

            assert "error" not in result, result
            assert result["value"].status_code == 404, result["value"].text
            assert result["value"].json()["detail"] == "Unknown user"
            assert users_in(db, chat) == [member.id]
            # So when the owner leaves, the chat goes to whoever's still in the organisation.
            add_user(api, chat, user)
            assert api.post("/api/organisation/leave").status_code == 200
        assert owners(api, chat) == [user.id]
    finally:
        delete_user(db, carol)


def test_a_chat_made_as_its_maker_leaves_the_organisation_isnt_made(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Carol makes a chat (in another tab, say) while her leave is under way: the request was for
    the organisation she's leaving. Made, it would be owned by someone it doesn't exist for."""
    carol = join_organisation(db, user, "carol")
    try:
        with signed_in_as(carol):
            with leaving(carol):
                making, result = in_the_background(
                    lambda: api.post("/api/chats", json={"name": "Mine", "visibility": "private"})
                )
                waits_on_a_lock(making)
            making.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 404, result["value"].text
        assert api.get("/api/chats").json() == []
        assert api.post("/api/chats", json={"name": "Mine"}).status_code == 201
    finally:
        delete_user(db, carol)


def test_a_chat_joined_as_its_joiner_leaves_the_organisation_isnt_joined(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Carol joins a public chat while her leave is under way. Joined, she'd be in a chat of an
    organisation she isn't in, and the member added first when its owner leaves."""
    carol = join_organisation(db, user, "carol")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Open", visibility="public")
        with signed_in_as(carol):
            with leaving(carol):
                joining, result = in_the_background(
                    lambda: api.post(f"/api/chats/{chat['id']}/join")
                )
                waits_on_a_lock(joining)
            joining.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 404, result["value"].text
        assert users_in(db, chat) == [member.id]
    finally:
        delete_user(db, carol)


def test_a_member_who_leaves_as_theyre_added_to_a_chat_is_taken_out_of_it(
    api,
    db,
    user,
    member,  # noqa: F811
    monkeypatch,
):
    """The other order: the add has seen Carol a member, and holds that, when her leave starts.
    The leave waits for the add (it doesn't deadlock with it), then takes her out of the chat."""
    carol = join_organisation(db, user, "carol")
    checked, go_on = threading.Event(), threading.Event()
    holds_member = organisations.holds_member

    def pausing(db, org, user_id):
        held = holds_member(db, org, user_id)
        if user_id == carol.id:
            checked.set()
            assert go_on.wait(10)
        return held

    monkeypatch.setattr(organisations, "holds_member", pausing)
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Plans", visibility="private")
            adding, added = in_the_background(
                lambda: api.post(
                    f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": carol.id}
                )
            )
            assert checked.wait(10), added
        with signed_in_as(carol):
            leaving_it, left = in_the_background(lambda: api.post("/api/organisation/leave"))
            waits_on_a_lock(leaving_it)
            go_on.set()
            adding.join(timeout=10)
            leaving_it.join(timeout=10)

        assert added["value"].status_code == 201, added
        assert left["value"].status_code == 200, left
        assert users_in(db, chat) == [member.id]
    finally:
        go_on.set()
        delete_user(db, carol)


def test_a_leave_holds_up_nothing_in_a_chat_the_leaver_didnt_own(
    api,
    db,
    user,
    member,  # noqa: F811
    run_service,
):
    """A chat left with nobody to own it before (its owner left with nobody in it) has nothing to
    do with Carol's leaving: reading it while her leave is under way, with the run its last
    member's mention started still waiting, doesn't wait for her leave to end. Only the chats
    she owned can have lost their owner by it."""

    run_service.auto = False
    carol = join_organisation(db, user, "carol")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Old", visibility="public")
            workflow = make_workflow(api, "Digest")
            add(api, chat, "workflow", workflow)
            resp = api.post(
                f"/api/chats/{chat['id']}/messages",
                json={"body": "Go", "mentions": [{"kind": "workflow", "id": workflow["id"]}]},
            )
            assert resp.status_code == 201, resp.text
            [invocation] = resp.json()["invocations"]
            assert api.post("/api/organisation/leave").status_code == 200
        assert owners(api, chat) == []
        with leaving(carol):
            reading, read = in_the_background(lambda: api.get(f"/api/chats/{chat['id']}/messages"))
            finishes_without_waiting(reading)

        assert "error" not in read, read
        assert read["value"].status_code == 200, read["value"].text
        [waiting] = read["value"].json()["invocations"]
        assert (waiting["id"], waiting["status"]) == (invocation["id"], "queued")
    finally:
        delete_user(db, carol)


def test_a_member_removed_from_a_chat_as_they_leave_the_organisation_is_removed(
    api,
    db,
    user,
):
    """The owner removes Carol as her leave is under way: the removal read her member row before
    her leave deleted it, and waits for the leave, which commits first. She's out of the chat
    either way, which is what the owner asked for: the removal is done, not an error, and
    doesn't delete a row that's no longer there (the ORM's delete of it would match no row: a
    warning, or `StaleDataError` were the rows versioned)."""
    carol = join_organisation(db, user, "carol")
    try:
        chat = make_chat(api, "Plans", visibility="private")
        [theirs] = [
            m for m in add_user(api, chat, carol)["members"]["users"] if m["target_id"] == carol.id
        ]
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            with leaving(carol):
                removing, removed = in_the_background(
                    lambda: api.delete(f"/api/chats/{chat['id']}/members/{theirs['id']}")
                )
                waits_on_a_lock(removing)
            removing.join(timeout=10)

        assert "error" not in removed, removed
        assert removed["value"].status_code == 204, removed["value"].text
        assert [str(w.message) for w in warned] == []
        assert users_in(db, chat) == [user.id]
    finally:
        delete_user(db, carol)


def test_a_member_who_leaves_a_chat_as_they_leave_the_organisation_is_out_of_it(
    api,
    db,
    user,
):
    """Carol leaves a chat (in another tab, say) as her leave of the organisation is under way:
    leaving the chat read her member row before her leave deleted it, and waits for the leave.
    She's out of it either way: done, and nothing deleted that's no longer there."""
    carol = join_organisation(db, user, "carol")
    try:
        chat = make_chat(api, "Plans", visibility="public")
        add_user(api, chat, carol)
        with signed_in_as(carol), warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            with leaving(carol):
                quitting, quit = in_the_background(
                    lambda: api.post(f"/api/chats/{chat['id']}/leave")
                )
                waits_on_a_lock(quitting)
            quitting.join(timeout=10)

        assert "error" not in quit, quit
        assert quit["value"].status_code == 204, quit["value"].text
        assert [str(w.message) for w in warned] == []
        assert users_in(db, chat) == [user.id]
    finally:
        delete_user(db, carol)


def test_a_chat_deleted_as_one_of_its_members_leaves_the_organisation_is_deleted(
    api,
    db,
    user,
):
    """The owner deletes the chat as Carol's leave is under way: the deletion read the chat's
    members, Carol among them, before her leave deleted her row, and waits for the leave. The chat
    goes, with nothing deleted that's no longer there."""
    carol = join_organisation(db, user, "carol")
    try:
        chat = make_chat(api, "Plans", visibility="private")
        add_user(api, chat, carol)
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            with leaving(carol):
                deleting, deleted = in_the_background(
                    lambda: api.delete(f"/api/chats/{chat['id']}")
                )
                waits_on_a_lock(deleting)
            deleting.join(timeout=10)

        assert "error" not in deleted, deleted
        assert deleted["value"].status_code == 204, deleted["value"].text
        assert [str(w.message) for w in warned] == []
        assert users_in(db, chat) == []
        assert api.get(f"/api/chats/{chat['id']}").status_code == 404
    finally:
        delete_user(db, carol)


def test_a_post_to_a_chat_deleted_as_its_owner_leaves_the_organisation_is_a_404(
    api,
    db,
    member,  # noqa: F811
):
    """Its owner posts in a private chat with nobody else in it (in another tab, say) as their
    leave is under way: the post read them a member before the leave deleted their row, and the
    leave, finding nobody to hand the chat to, deletes it. The post waits for the leave and finds
    the chat gone: a 404, as for a chat deleted any other way, not a message in no chat."""
    with signed_in_as(member):
        chat = make_chat(api, "Mine", visibility="private")
        with leaving(member):
            posting, posted = in_the_background(
                lambda: api.post(f"/api/chats/{chat['id']}/messages", json={"body": "Bye"})
            )
            waits_on_a_lock(posting)
        posting.join(timeout=10)

    assert "error" not in posted, posted
    assert posted["value"].status_code == 404, posted["value"].text
    assert is_gone(db, chat)


def test_a_change_to_a_chat_deleted_as_its_owner_leaves_the_organisation_is_a_404(
    api,
    db,
    member,  # noqa: F811
):
    """The same with its owner renaming it: the change waits for the leave and finds the chat
    gone, a 404, not an update of a row that's no longer there."""
    with signed_in_as(member):
        chat = make_chat(api, "Mine", visibility="private")
        body = {"name": "Renamed", "description": "", "visibility": "private"}
        with leaving(member):
            changing, changed = in_the_background(
                lambda: api.put(f"/api/chats/{chat['id']}", json=body)
            )
            waits_on_a_lock(changing)
        changing.join(timeout=10)

    assert "error" not in changed, changed
    assert changed["value"].status_code == 404, changed["value"].text
    assert is_gone(db, chat)
