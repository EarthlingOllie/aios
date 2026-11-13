"""Chats belong to the organisation: every member of it reads and joins them, a chat's members add
any of its agents and workflows, names are unique within it, each new chat gets its documents
assistant, and to anyone in another organisation its chats don't exist."""

import threading
import time
import warnings
from contextlib import contextmanager

from conftest import delete_user, home_organisation, join_organisation, signed_in_store
from sqlalchemy import select, text
from test_chats import add, join_as, make_agent, make_chat, make_workflow, post, signed_in_as
from test_organisation_scoping import member, outsider, person  # noqa: F401
from test_organisations import in_the_background

from aios import organisations
from aios.datastores import store_session
from aios.datastores.models import ChatMember
from aios.db import SessionLocal, engine
from aios.models import (
    ChatMemberKind,
    User,
)

NAME_TAKEN = "The organisation already has a chat with that name."


def every_endpoint(api, chat: dict, user_id: int, member_id: int = 1) -> dict[str, int]:
    """The status of each of the chat's endpoints for the signed-in user, adding the user
    `user_id` (which nobody may) and removing the member row `member_id`."""
    url = f"/api/chats/{chat['id']}"
    body = {"name": "Mine now", "description": ""}
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
        "archive": api.post(f"{url}/archive"),
        "unarchive": api.post(f"{url}/unarchive"),
    }
    return {what: r.status_code for what, r in responses.items()}


def test_another_organisation_cant_reach_a_chat(api, user, outsider):  # noqa: F811
    chat = make_chat(api, "Open")
    post(api, chat, "Hello")
    [first] = chat["members"]["users"]
    with signed_in_as(outsider):
        assert api.get("/api/chats").json() == []
        got = every_endpoint(api, chat, outsider.id, first["id"])
        assert got == {what: 404 for what in got}
    # Untouched.
    assert [c["name"] for c in api.get("/api/chats").json()] == ["Open"]


def test_every_chat_is_every_member_of_the_organisations(api, user, member):  # noqa: F811
    chat = make_chat(api, "Open")
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
        body = {"name": "general", "description": ""}
        resp = api.put(f"/api/chats/{mine['id']}", json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"] == NAME_TAKEN
    # Another organisation has a "general" of its own.
    with signed_in_as(outsider):
        theirs = make_chat(api, "general")
        assert [c["id"] for c in api.get("/api/chats").json()] == [theirs["id"]]


def test_a_member_adds_another_members_agent_and_workflow(
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
    chat = join_as(api, chat, member)
    assert [m["name"] for m in chat["members"]["agents"]] == [
        "Organisation assistant",
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
    for kind, target in (("agent", theirs), ("workflow", their_workflow)):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": target["id"]})
        assert resp.status_code == 422, kind
        assert resp.json()["detail"] == f"Unknown {kind}"
    assert api.get(f"/api/chats/{chat['id']}").json()["member_count"] == 5


def test_a_new_chats_first_agent_is_the_organisations_assistant(
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
    assert assistant["name"] == "Organisation assistant"
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
    chat = make_chat(api, "Design")
    assert (chat["created_by"], chat["updated_by"]) == (person(user), person(user))
    with signed_in_as(member):
        [listed] = api.get("/api/chats").json()
        assert (listed["created_by"], listed["updated_by"]) == (person(user), person(user))
        assert api.post(f"/api/chats/{chat['id']}/join").status_code == 200
        body = {"name": "Product design", "description": "Pixels"}
        changed = api.put(f"/api/chats/{chat['id']}", json=body).json()
    assert (changed["created_by"], changed["updated_by"]) == (person(user), person(member))
    assert api.get(f"/api/chats/{chat['id']}").json()["created_by"] == person(user)


def test_a_chat_whose_last_person_leaves_the_organisation_stays_for_anyone_to_join(
    api,
    user,
    member,  # noqa: F811
):
    with signed_in_as(member):
        chat = make_chat(api, "Abandoned")
        hello = post(api, chat, "Notes for the team")
        assert api.post("/api/organisation/leave").status_code == 200

    [listed] = api.get("/api/chats").json()
    assert (listed["name"], listed["is_member"]) == ("Abandoned", False)
    assert api.get(f"/api/chats/{chat['id']}/messages").json()["messages"][0]["id"] == hello["id"]
    joined = api.post(f"/api/chats/{chat['id']}/join").json()
    assert [m["target_id"] for m in joined["members"]["users"]] == [user.id]


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
    flushed within the block (their chat member rows deleted, their membership of the
    organisation locked) and committed as it ends."""
    with SessionLocal() as session:
        user = session.get_one(User, leaver.id)
        org = home_organisation(session, user.id)
        assert organisations.leave_refusal(session, org, user) is None
        organisations.leave(session, org, user)
        session.flush()
        yield
        session.commit()


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


def test_a_chat_made_as_its_maker_leaves_the_organisation_isnt_made(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Carol makes a chat (in another tab, say) while her leave is under way: the request was for
    the organisation she's leaving. Made, she'd be in a chat of an organisation she isn't in."""
    carol = join_organisation(db, user, "carol")
    try:
        with signed_in_as(carol):
            with leaving(carol):
                making, result = in_the_background(
                    lambda: api.post("/api/chats", json={"name": "Mine"})
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
    """Carol joins a chat while her leave is under way. Joined, she'd be in a chat of an
    organisation she isn't in."""
    carol = join_organisation(db, user, "carol")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Open")
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


def test_a_leave_holds_up_nothing_in_a_chat_the_leaver_isnt_in(
    api,
    db,
    user,
    member,  # noqa: F811
    run_service,
):
    """A chat whose last person left before has nothing to do with Carol's leaving: reading it
    while her leave is under way, with the run its last member's mention started still waiting,
    doesn't wait for her leave to end."""

    run_service.auto = False
    carol = join_organisation(db, user, "carol")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Old")
            workflow = make_workflow(api, "Digest")
            add(api, chat, "workflow", workflow)
            resp = api.post(
                f"/api/chats/{chat['id']}/messages",
                json={"body": "Go", "mentions": [{"kind": "workflow", "id": workflow["id"]}]},
            )
            assert resp.status_code == 201, resp.text
            [invocation] = resp.json()["invocations"]
            assert api.post("/api/organisation/leave").status_code == 200
        with leaving(carol):
            reading, read = in_the_background(lambda: api.get(f"/api/chats/{chat['id']}/messages"))
            finishes_without_waiting(reading)

        assert "error" not in read, read
        assert read["value"].status_code == 200, read["value"].text
        [waiting] = read["value"].json()["invocations"]
        assert (waiting["id"], waiting["status"]) == (invocation["id"], "queued")
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
        chat = make_chat(api, "Plans")
        join_as(api, chat, carol)
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


def test_a_post_by_someone_as_they_leave_the_organisation_isnt_posted(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """They post (in another tab, say) as their leave is under way: the request was for the
    organisation they're leaving. The post waits for the leave and finds them gone from it: a
    404, and nothing posted by someone outside it, nor them joined to the chat by it."""
    chat = make_chat(api, "Open")
    with signed_in_as(member):
        with leaving(member):
            posting, posted = in_the_background(
                lambda: api.post(f"/api/chats/{chat['id']}/messages", json={"body": "Bye"})
            )
            waits_on_a_lock(posting)
        posting.join(timeout=10)

    assert "error" not in posted, posted
    assert posted["value"].status_code == 404, posted["value"].text
    assert api.get(f"/api/chats/{chat['id']}/messages").json()["messages"] == []
    assert users_in(db, chat) == [user.id]
