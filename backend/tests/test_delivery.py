"""Schedules can deliver their results to a chat: summarised when the run ends, or a short
failure message; nothing is delivered to an archived chat, and its setter is told."""

from datetime import UTC, datetime, timedelta

from test_chat_agent_invocations import replies_from
from test_chat_archiving import archive
from test_chats import make_chat, messages, other, signed_in_as  # noqa: F401 (a fixture)
from test_notifications import unread_of
from test_scheduler import drain_run, make_due, make_runnable_workflow, queued, tick  # noqa: F401
from test_triggers import make_agent, make_trigger, schedule
from test_workflow_runs import echo

from aios.testing.fake_model import ToolCall, model, replies, summarises, use_handback, use_model


def members(api, chat: dict) -> set[tuple[str, int]]:
    resp = api.get(f"/api/chats/{chat['id']}")
    assert resp.status_code == 200, resp.text
    found = resp.json()["members"]
    return {(m["kind"], m["target_id"]) for m in [*found["agents"], *found["workflows"]]}


def fire(db, trigger: dict) -> None:
    now = datetime.now(UTC)
    make_due(db, trigger["id"], now - timedelta(seconds=1))
    tick(now)


def test_choosing_a_chat_to_deliver_to_adds_the_agent_and_records_who_chose_it(
    api,
    user,
    other,  # noqa: F811
):
    # A chat its setter isn't in.
    with signed_in_as(other):
        chat = make_chat(api, "Ops")
    agent = make_agent(api, "Newsbot")
    assert ("agent", agent["id"]) not in members(api, chat)

    t = make_trigger(api, schedule("agent", agent["id"], deliver_to=chat["id"]))

    assert t["deliver_to"] == {"id": chat["id"], "name": "Ops", "archived": False}
    assert t["deliver_to_set_by"]["id"] == user.id
    assert t["delivery_problem"] is None
    assert ("agent", agent["id"]) in members(api, chat)
    # Changing something else keeps it, and who chose it; choosing none clears it.
    with signed_in_as(other):
        resp = api.put(
            f"/api/triggers/{t['id']}",
            json=schedule("agent", agent["id"], deliver_to=chat["id"], input="Changed"),
        )
    assert resp.json()["deliver_to_set_by"]["id"] == user.id
    resp = api.put(f"/api/triggers/{t['id']}", json=schedule("agent", agent["id"]))
    assert (resp.json()["deliver_to"], resp.json()["deliver_to_set_by"]) == (None, None)


def test_a_workflow_schedule_delivering_to_a_chat_adds_the_workflow(api):
    workflow = make_runnable_workflow(api)
    chat = make_chat(api, "Ops")
    make_trigger(api, schedule("workflow", workflow["id"], deliver_to=chat["id"]))
    assert ("workflow", workflow["id"]) in members(api, chat)


def test_choosing_an_archived_chat_is_refused_with_why(api):
    agent = make_agent(api, "Newsbot")
    chat = make_chat(api, "Old")
    archive(api, chat)

    resp = api.post("/api/triggers", json=schedule("agent", agent["id"], deliver_to=chat["id"]))

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Old is archived, so results can't be delivered there. Choose another chat."
    )
    # An unknown chat is bad input.
    resp = api.post("/api/triggers", json=schedule("agent", agent["id"], deliver_to=999_999))
    assert resp.status_code == 422


def test_a_successful_scheduled_run_posts_one_summary_when_it_ends(api, db):
    agent = make_agent(api, "Newsbot")
    chat = make_chat(api, "Ops")
    t = make_trigger(api, schedule("agent", agent["id"], deliver_to=chat["id"]))
    mid_turn = []

    async def script(msgs, info):
        # Nothing is posted when the run starts.
        mid_turn.append(replies_from(api, chat))
        yield "Three stories today: a, b and c, each in detail."

    with use_model(model(script)), use_handback(summarises("Three stories: a, b, c.")):
        fire(db, t)

    assert mid_turn == [[]]
    assert replies_from(api, chat) == [("Newsbot", "Three stories: a, b, c.")]
    read = messages(api, chat)
    [posted] = [m for m in read["messages"] if m["author"] and m["author"]["kind"] == "agent"]
    # With its run's card, collapsed under it: the full output behind it.
    [invocation] = read["invocations"]
    assert posted["invocation_id"] == invocation["id"]
    assert (invocation["delivered"], invocation["status"]) == (True, "succeeded")
    assert invocation["full_output"] == "Three stories today: a, b and c, each in detail."
    assert invocation["summary"] == "Three stories: a, b, c."
    assert (
        invocation["agent_run"]["id"] == api.get(f"/api/triggers/{t['id']}").json()["last_run_id"]
    )


def test_the_handback_of_a_delivery_is_asked_with_the_schedules_message(api, db):
    agent = make_agent(api, "Newsbot")
    chat = make_chat(api, "Ops")
    t = make_trigger(
        api, schedule("agent", agent["id"], deliver_to=chat["id"], input="What happened?")
    )
    seen = []

    async def script(msgs, info):
        seen.append("".join(m.content for m in msgs))
        yield ToolCall("Handback", {"summary": "Not much."})

    with use_model(replies("Nothing at all.")), use_handback(model(script)):
        fire(db, t)

    [sent] = seen
    assert '<untrusted source="trigger message">\nWhat happened?\n</untrusted>' in sent
    assert '<untrusted source="output">\nNothing at all.\n</untrusted>' in sent


def test_a_failed_scheduled_run_posts_one_failure_message_and_notifies(
    api,
    db,
    user,
    other,  # noqa: F811
    queued,  # noqa: F811
):
    # Made by another member (an editor), delivering to a chat its creator isn't in.
    workflow = make_runnable_workflow(api)
    chat = make_chat(api, "Ops")
    with signed_in_as(other):
        t = make_trigger(api, schedule("workflow", workflow["id"], deliver_to=chat["id"]))
    fire(db, t)
    with use_model(echo(fail={"echo"})):
        drain_run(queued.store, queued[-1])

    [(name, body)] = replies_from(api, chat, "workflow")
    assert name == "Flow"
    assert body.startswith("The run failed: ") and "echo broke" in body
    result = [m for m in messages(api, chat)["messages"] if m["kind"] == "text"][-1]
    # Its creator and the admins are told.
    for person in (other, user):
        with signed_in_as(person):
            [failed] = unread_of(api, "run_failed")
        assert failed["ref"] == {"chat_id": chat["id"], "message_id": result["id"]}


def test_after_the_chat_is_archived_the_run_runs_and_nothing_is_delivered(
    api,
    db,
    user,
    other,  # noqa: F811
):
    agent = make_agent(api, "Newsbot")
    chat = make_chat(api, "Ops")
    with signed_in_as(other):
        t = make_trigger(api, schedule("agent", agent["id"], deliver_to=chat["id"]))
    archive(api, chat)
    ran = []

    async def script(msgs, info):
        ran.append(True)
        yield "Done."

    with use_model(model(script)):
        fire(db, t)

    assert ran == [True]
    assert replies_from(api, chat) == []
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert shown["last_status"] == "succeeded"
    assert shown["deliver_to"]["archived"] is True
    assert shown["delivery_problem"] == (
        "Ops is archived, so the result wasn't delivered. Choose another chat."
    )
    # Whoever chose the chat is told; nobody else.
    with signed_in_as(other):
        [told] = unread_of(api, "run_failed")
    assert told["ref"]["undelivered"] is True
    assert told["title"] == "Newsbot's result wasn't delivered"
    assert told["detail"] == shown["delivery_problem"]
    assert unread_of(api, "run_failed") == []
    # Choosing another chat clears the problem.
    chat2 = make_chat(api, "Ops 2")
    resp = api.put(
        f"/api/triggers/{t['id']}", json=schedule("agent", agent["id"], deliver_to=chat2["id"])
    )
    assert resp.json()["delivery_problem"] is None
