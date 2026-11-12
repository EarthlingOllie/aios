"""In-app notifications, the person's from every organisation they're in: an approval needed
notifies whoever can decide it, and deciding it clears them; mentions, replies and failed runs
notify whoever they're for; reading a chat past a message clears those about it. Each person
only ever sees their own."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import delete_user, join_organisation, org_of, take_turn
from sqlalchemy import select
from sse import open_stream
from test_chat_agent_invocations import say
from test_chats import (  # noqa: F401 (fixtures)
    add,
    make_agent,
    make_chat,
    messages,
    other,
    outsider,
    post,
    signed_in_as,
)
from test_schema_change_requests import CONTACTS, Script, agent, queued, start, store  # noqa: F401

from aios import approvals, notifications, worker
from aios.auth import current_user
from aios.config import get_settings
from aios.main import app
from aios.models import Notification, OrganisationMember
from aios.testing.fake_model import model, replies, use_model


def test_marking_one_read_is_idempotent(api, agent, queued):  # noqa: F811
    start(api, agent, Script(CONTACTS))
    [notification] = api.get("/api/me/notifications").json()["notifications"]

    first = api.post(f"/api/me/notifications/{notification['id']}/read")
    again = api.post(f"/api/me/notifications/{notification['id']}/read")

    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json()["read_at"] is not None
    # Read once: the second call changes nothing.
    assert again.json() == first.json()
    assert api.get("/api/me/notifications").json()["unread"] == 0


def test_unread_come_first_newest_first(api, agent, queued):  # noqa: F811
    _, first = start(api, agent, Script(CONTACTS))
    _, second = start(api, agent, Script({**CONTACTS, "name": "places"}))
    _, third = start(api, agent, Script({**CONTACTS, "name": "things"}))
    api.post(f"/api/approvals/{third['id']}/reject", json={})

    listed = api.get("/api/me/notifications").json()

    assert listed["unread"] == 2
    assert [n["ref"]["approval_id"] for n in listed["notifications"]] == [
        second["id"],
        first["id"],
        third["id"],
    ]


def test_users_only_see_and_mark_their_own(api, agent, queued, new_user):  # noqa: F811
    start(api, agent, Script(CONTACTS))
    [mine] = api.get("/api/me/notifications").json()["notifications"]
    someone = new_user()
    app.dependency_overrides[current_user] = lambda: someone

    assert api.get("/api/me/notifications").json() == {"unread": 0, "notifications": []}
    assert api.post(f"/api/me/notifications/{mine['id']}/read").status_code == 404
    assert api.post(f"/api/me/notifications/{uuid.uuid4()}/read").status_code == 404


def test_requesting_a_change_notifies_the_owner(api, agent, store, queued):  # noqa: F811
    _, request = start(api, agent, Script(CONTACTS))

    listed = api.get("/api/me/notifications").json()

    assert listed["unread"] == 1
    [notification] = listed["notifications"]
    assert notification["kind"] == "approval_needed"
    # Naming the approval, never what it says.
    assert notification["ref"] == {"approval_id": request["id"]}
    assert notification["read_at"] is None


def test_a_notification_says_which_organisation_its_from(api, db, user, agent, queued):  # noqa: F811
    """Notifications are the person's, from every organisation they're in: each names its own, so
    opening it goes there."""
    start(api, agent, Script(CONTACTS))

    [notification] = api.get("/api/me/notifications").json()["notifications"]

    org = org_of(db, user)
    assert notification["organisation"] == {"id": org.id, "slug": org.slug, "name": org.name}


def test_a_requests_notification_says_what_it_asks_for(api, agent, queued):  # noqa: F811
    """So the list reads without opening each one."""
    _, request = start(api, agent, Script(CONTACTS))

    [notification] = api.get("/api/me/notifications").json()["notifications"]

    assert notification["title"] == "A schema change is waiting for you"
    assert notification["detail"] == request["summary"] == "Create table data.contacts"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_deciding_the_request_marks_its_notification_read(api, agent, queued, decision):  # noqa: F811
    _, request = start(api, agent, Script(CONTACTS))

    assert api.post(f"/api/approvals/{request['id']}/{decision}", json={}).status_code == 200

    listed = api.get("/api/me/notifications").json()
    assert listed["unread"] == 0
    [notification] = listed["notifications"]
    assert notification["read_at"] is not None


def test_a_decision_and_clearing_its_notifications_commit_together(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
    monkeypatch,
):
    """Clearing them can't fail after the decision is made, leaving a 500 for a decided
    request (which a retry answers 409) and its notification unread."""
    _, request = start(api, agent, Script(CONTACTS))

    def lost_connection(*args, **kwargs):
        raise RuntimeError("lost the connection")

    monkeypatch.setattr(approvals, "clear_notifications", lost_connection)
    with pytest.raises(RuntimeError):
        api.post(f"/api/approvals/{request['id']}/approve")
    monkeypatch.undo()

    after = api.get(f"/api/approvals/{request['id']}").json()
    assert (after["status"], queued) == ("pending", [])
    assert api.get("/api/me/notifications").json()["unread"] == 1


def test_a_decision_that_cant_be_queued_leaves_the_notification_unread(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
    monkeypatch,
):
    """The decision is undone (503), so the request still waits for one."""
    _, request = start(api, agent, Script(CONTACTS))

    def broker_down(*args):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(worker.apply_schema_change_task, "delay", broker_down)
    assert api.post(f"/api/approvals/{request['id']}/approve").status_code == 503

    assert api.get("/api/me/notifications").json()["unread"] == 1


def test_an_undone_decision_leaves_one_the_user_read_themselves_read(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
    monkeypatch,
):
    _, request = start(api, agent, Script(CONTACTS))
    [notification] = api.get("/api/me/notifications").json()["notifications"]
    read = api.post(f"/api/me/notifications/{notification['id']}/read").json()

    def broker_down(*args):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(worker.apply_schema_change_task, "delay", broker_down)
    assert api.post(f"/api/approvals/{request['id']}/approve").status_code == 503

    listed = api.get("/api/me/notifications").json()
    assert listed["unread"] == 0
    assert listed["notifications"][0]["read_at"] == read["read_at"]


# --- Mentions, replies and failures, across organisations -----------------------------------------


def listed(api) -> dict:
    resp = api.get("/api/me/notifications")
    assert resp.status_code == 200, resp.text
    return resp.json()


def unread_of(api, kind: str | None = None) -> list[dict]:
    return [
        n
        for n in listed(api)["notifications"]
        if n["read_at"] is None and (kind is None or n["kind"] == kind)
    ]


def mention(api, chat: dict, body: str, *people) -> dict:
    """Post `body` mentioning the people; returns the message."""
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": body, "mentions": [{"kind": "user", "id": p.id} for p in people]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["messages"][0]


def read_to(api, chat: dict, message_id: int) -> None:
    resp = api.post(f"/api/chats/{chat['id']}/read", json={"message_id": message_id})
    assert resp.status_code == 204, resp.text


def test_a_mention_notifies_the_people_mentioned_never_the_poster(api, db, user, other):  # noqa: F811
    chat = make_chat(api, "Design")
    with signed_in_as(other):
        assert api.post(f"/api/chats/{chat['id']}/join").status_code == 200
    message = mention(
        api,
        chat,
        f"@[Other Person](user:{other.id}) @[Test User](user:{user.id}) look",
        other,
        user,
    )

    assert unread_of(api) == []
    with signed_in_as(other):
        [told] = unread_of(api)
    org = org_of(db, user)
    assert told["kind"] == "mention"
    assert told["ref"] == {"chat_id": chat["id"], "message_id": message["id"]}
    assert told["organisation"] == {"id": org.id, "slug": org.slug, "name": org.name}
    # Read from the store as it's listed.
    assert told["title"] == "Test User mentioned you in Design"
    assert told["detail"] == "@Other Person @Test User look"


def test_a_reply_notifies_whoever_mentioned_the_agent(api, user):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)

    with use_model(replies("All done.")):
        say(api, chat, f"@[Bot](agent:{bot['id']}) go", ("agent", bot))

    [reply] = unread_of(api)
    result = messages(api, chat)["messages"][-1]
    assert (reply["kind"], reply["ref"]) == (
        "reply",
        {"chat_id": chat["id"], "message_id": result["id"]},
    )
    assert (reply["title"], reply["detail"]) == ("Bot replied in Design", "All done.")


def test_no_reply_notification_while_the_asker_watches_the_chat_in_a_visible_tab(api):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)

    with (
        open_stream(api, f"/api/chats/{chat['id']}/stream?visible=true") as stream,
        use_model(replies("Seen it.")),
    ):
        # Open, and so watching.
        assert stream.status == 200
        say(api, chat, f"@[Bot](agent:{bot['id']}) go", ("agent", bot))
        assert unread_of(api) == []

    # A hidden tab's stream isn't watching.
    with (
        open_stream(api, f"/api/chats/{chat['id']}/stream?visible=false"),
        use_model(replies("Missed it.")),
    ):
        say(api, chat, f"@[Bot](agent:{bot['id']}) again", ("agent", bot))
        [reply] = unread_of(api)
    assert reply["detail"] == "Missed it."


def test_a_failed_mention_notifies_whoever_mentioned_it(api):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)

    async def breaks(msgs, info):
        yield "Starting"
        raise RuntimeError("model overloaded")

    # Even watching: a failure is told.
    with (
        open_stream(api, f"/api/chats/{chat['id']}/stream?visible=true"),
        use_model(model(breaks)),
    ):
        say(api, chat, f"@[Bot](agent:{bot['id']}) go", ("agent", bot))
        [failed] = unread_of(api)
    result = messages(api, chat)["messages"][-1]
    assert (failed["kind"], failed["ref"]) == (
        "run_failed",
        {"chat_id": chat["id"], "message_id": result["id"]},
    )
    assert failed["title"] == "Bot failed in Design"
    assert failed["detail"] == "The run failed: model overloaded"


def test_a_run_started_by_hand_that_fails_notifies_whoever_started_it(api, other):  # noqa: F811
    bot = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{bot['id']}/runs").json()

    async def breaks(msgs, info):
        yield "Starting"
        raise RuntimeError("model overloaded")

    with use_model(model(breaks)):
        take_turn(api, bot["id"], run["id"], "go")

    [failed] = unread_of(api)
    assert (failed["kind"], failed["ref"]) == (
        "run_failed",
        {"agent_id": bot["id"], "run_id": run["id"]},
    )
    assert (failed["title"], failed["detail"]) == ("A run of Bot failed", "model overloaded")
    # Only them.
    with signed_in_as(other):
        assert unread_of(api) == []


def test_repeats_about_the_same_thing_never_make_a_second_unread_one(api, db, user):
    org = org_of(db, user).id
    for _ in range(2):
        notifications.notify(
            db, [user.id], org, notifications.MENTION, {"chat_id": 1, "message_id": 2}
        )
        db.commit()
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id)).all()
    assert len(rows) == 1
    # Once read, a new one about it can come.
    notifications.mark_all_read(db, user.id)
    notifications.notify(db, [user.id], org, notifications.MENTION, {"chat_id": 1, "message_id": 2})
    db.commit()
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id)).all()
    assert sorted(r.read_at is None for r in rows) == [False, True]


def test_reading_a_chat_past_a_message_clears_the_notifications_about_it(api, user, other):  # noqa: F811
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    with signed_in_as(other):
        api.post(f"/api/chats/{chat['id']}/join")
        first = mention(api, chat, f"@[Test User](user:{user.id}) one", user)
    with use_model(replies("Done.")):
        say(api, chat, f"@[Bot](agent:{bot['id']}) go", ("agent", bot))
    with signed_in_as(other):
        second = mention(api, chat, f"@[Test User](user:{user.id}) two", user)
    assert {n["kind"] for n in unread_of(api)} == {"mention", "reply"}

    # Up to the first mention: only that one.
    read_to(api, chat, first["id"])
    assert sorted(n["kind"] for n in unread_of(api)) == ["mention", "reply"]
    assert [n["ref"]["message_id"] for n in unread_of(api, "mention")] == [second["id"]]
    # To the end: all of them.
    read_to(api, chat, second["id"])
    assert listed(api)["unread"] == 0


def test_mark_all_read_clears_everything(api, user, other):  # noqa: F811
    chat = make_chat(api, "Design")
    with signed_in_as(other):
        api.post(f"/api/chats/{chat['id']}/join")
        for word in ("one", "two"):
            mention(api, chat, f"@[Test User](user:{user.id}) {word}", user)
    assert listed(api)["unread"] == 2

    assert api.post("/api/me/notifications/read").status_code == 204

    after = listed(api)
    assert after["unread"] == 0
    assert len(after["notifications"]) == 2


def test_the_bell_lists_every_organisations_each_labelled(api, db, user, outsider):  # noqa: F811
    """`outsider` is in an organisation of their own and joins the user's: mentioned in each."""
    ours = org_of(db, user)
    theirs = org_of(db, outsider)
    db.add(OrganisationMember(organisation_id=ours.id, user_id=outsider.id))
    db.commit()
    chat = make_chat(api, "Ours")
    with signed_in_as(outsider):
        assert api.post(f"/api/o/{ours.id}/chats/{chat['id']}/join").status_code == 200
        own = make_chat(api, "Theirs")
    mention(api, chat, f"@[Outsider](user:{outsider.id}) here", outsider)
    # Someone else in their own organisation mentions them there.
    mate = join_organisation(db, outsider, "mate", first_name="Mate", last_name="Person")
    db.commit()
    try:
        with signed_in_as(mate):
            api.post(f"/api/o/{theirs.id}/chats/{own['id']}/join")
            resp = api.post(
                f"/api/o/{theirs.id}/chats/{own['id']}/messages",
                json={
                    "body": f"@[Outsider](user:{outsider.id}) there",
                    "mentions": [{"kind": "user", "id": outsider.id}],
                },
            )
            assert resp.status_code == 201, resp.text
        with signed_in_as(outsider):
            told = listed(api)["notifications"]
        assert {(n["organisation"]["name"], n["title"]) for n in told} == {
            (ours.name, "Test User mentioned you in Ours"),
            (theirs.name, "Mate Person mentioned you in Theirs"),
        }

        # Leaving ours deletes its notifications for them; theirs stay.
        with signed_in_as(outsider):
            resp = api.post(f"/api/o/{ours.id}/organisation/leave")
            assert resp.status_code == 200, resp.text
            told = listed(api)["notifications"]
        assert [n["organisation"]["name"] for n in told] == [theirs.name]
    finally:
        delete_user(db, mate)


def test_one_whose_reference_is_gone_is_dropped_and_marked_read(api, db, user):
    chat = make_chat(api, "Design")
    message = post(api, chat, "Here")
    org = org_of(db, user).id
    notifications.notify(
        db, [user.id], org, notifications.MENTION, notifications.chat_ref(chat["id"], message["id"])
    )
    notifications.notify(
        db, [user.id], org, notifications.MENTION, notifications.chat_ref(chat["id"], 10**9)
    )
    db.commit()

    after = listed(api)

    assert [n["ref"]["message_id"] for n in after["notifications"]] == [message["id"]]
    assert after["unread"] == 1
    db.expire_all()
    gone = db.scalar(
        select(Notification).where(Notification.ref["message_id"].astext == str(10**9))
    )
    assert gone.read_at is not None


def test_read_notifications_past_the_retention_are_pruned(api, db, user):
    org = org_of(db, user).id
    for n in range(3):
        notifications.notify(
            db, [user.id], org, notifications.MENTION, {"chat_id": 1, "message_id": n}
        )
    db.commit()
    old, recent, unread = db.scalars(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.ref)
    ).all()
    days = get_settings().notification_retention_days
    old.read_at = datetime.now(UTC) - timedelta(days=days + 1)
    recent.read_at = datetime.now(UTC) - timedelta(days=days - 1)
    db.commit()

    worker.prune_retention_task()

    db.expire_all()
    left = db.scalars(select(Notification.id).where(Notification.user_id == user.id)).all()
    assert sorted(left) == sorted([recent.id, unread.id])


def test_new_and_read_notifications_reach_the_person_stream(api, user, other):  # noqa: F811
    chat = make_chat(api, "Design")
    with signed_in_as(other):
        api.post(f"/api/chats/{chat['id']}/join")
    with open_stream(api, "/api/me/stream") as stream:
        assert stream.status == 200
        with signed_in_as(other):
            message = mention(api, chat, f"@[Test User](user:{user.id}) hi", user)
        stream.until(lambda es: any(e.event == "notification" for e in es))
        read_to(api, chat, message["id"])
        events = stream.until(lambda es: len([e for e in es if e.event == "notification"]) == 2)
    told = [e.data for e in events if e.event == "notification"]
    assert told == [{"change": "new", "unread": 1}, {"change": "read", "unread": 0}]
