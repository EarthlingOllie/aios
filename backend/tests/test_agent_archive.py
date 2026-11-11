"""Archiving an agent instead of deleting it: nothing is lost, and nothing new starts on it.

`POST /api/agents/{id}/archive` and `/unarchive` put it away and bring it back, never refused.
`GET /api/agents/{id}/used-by` is what the archive confirmation shows. An archived agent starts
no new turn by hand, by schedule, by mention or as part of a workflow, while what's already going
finishes; its schedules pause and stay paused after unarchiving; its name stays reserved.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from test_agents import chat_body
from test_chat_agent_invocations import say
from test_chats import add, make_chat, messages, signed_in_as
from test_organisation_scoping import member, outsider  # noqa: F401 (fixtures)
from test_scheduler import load, make_due, tick
from test_triggers import make_trigger, schedule
from test_workflow_runs import echo, queued, start, steps  # noqa: F401 (a fixture)
from test_workflows import agent_node, make_agent, make_workflow, workflow_node

from aios.models import Trigger, TriggerStatus
from aios.testing.fake_model import model, replies, use_handoff, use_model
from aios.workflows import execute_run


def archive(api, agent: dict) -> dict:
    resp = api.post(f"/api/agents/{agent['id']}/archive")
    assert resp.status_code == 200, resp.text
    return resp.json()


def unarchive(api, agent: dict) -> dict:
    resp = api.post(f"/api/agents/{agent['id']}/unarchive")
    assert resp.status_code == 200, resp.text
    return resp.json()


def refused_as_archived(resp, name: str) -> None:
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == f"The agent {name} is archived."


def test_archiving_and_unarchiving_an_agent(api, user):
    agent = make_agent(api, "Reviewer")
    assert (agent["archived_at"], agent["archived_by"]) == (None, None)

    archived = archive(api, agent)

    assert archived["archived_at"] is not None
    assert archived["archived_by"] == {"id": user.id, "name": user.full_name}
    # Its page still opens, archived, with its versions and runs.
    shown = api.get(f"/api/agents/{agent['id']}").json()
    assert shown["archived_at"] == archived["archived_at"]
    assert api.get(f"/api/agents/{agent['id']}/versions").status_code == 200
    assert api.get(f"/api/agents/{agent['id']}/runs").status_code == 200
    # Archiving again changes nothing, and is never refused.
    assert archive(api, agent)["archived_at"] == archived["archived_at"]

    back = unarchive(api, agent)

    assert (back["archived_at"], back["archived_by"]) == (None, None)
    assert api.post(f"/api/agents/{agent['id']}/runs").status_code == 201


def test_the_agents_list_hides_archived_agents_unless_asked_for_them(api):
    kept = make_agent(api, "Kept")
    put_away = make_agent(api, "Put away")
    archive(api, put_away)

    default = api.get("/api/agents").json()
    archived = api.get("/api/agents", params={"archived": "true"}).json()

    assert [a["name"] for a in default] == ["Kept"]
    assert default[0]["archived_at"] is None
    assert [a["name"] for a in archived] == ["Put away"]
    assert archived[0]["archived_at"] is not None
    unarchive(api, put_away)
    assert [a["id"] for a in api.get("/api/agents").json()] == [kept["id"], put_away["id"]]


def test_an_archived_agents_name_stays_reserved(api):
    agent = make_agent(api, "Reviewer")
    archive(api, agent)

    resp = api.post("/api/agents", json={"name": "Reviewer"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "The organisation already has an agent called Reviewer (archived)."
    )
    # And unarchiving it never clashes.
    assert unarchive(api, agent)["name"] == "Reviewer"


def test_a_turn_by_hand_is_refused_naming_the_agent(api):
    agent = make_agent(api, "Reviewer")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    archive(api, agent)

    # Neither a new run nor a new turn in a run it already has.
    refused_as_archived(api.post(f"/api/agents/{agent['id']}/runs"), "Reviewer")
    with use_model(replies("never")):
        turn = api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hi"))
    refused_as_archived(turn, "Reviewer")


def test_a_turn_already_going_finishes(api):
    agent = make_agent(api, "Reviewer")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    async def script(msgs, info):
        # Archived while the turn is underway.
        await asyncio.to_thread(archive, api, agent)
        yield "Done anyway."

    with use_model(model(script)):
        resp = api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hi"))
    assert resp.status_code == 200, resp.text
    assert "Done anyway." in resp.text
    out = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()
    assert out["status"] == "succeeded"
    assert out["message_count"] == 2


def test_its_schedules_pause_and_stay_paused_after_unarchiving(api, db):
    agent = make_agent(api, "Newsbot")
    daily = make_trigger(api, schedule("agent", agent["id"]))
    other = make_trigger(api, schedule("agent", make_agent(api, "Other")["id"]))

    archive(api, agent)

    assert api.get(f"/api/triggers/{daily['id']}").json()["enabled"] is False
    assert api.get(f"/api/triggers/{other['id']}").json()["enabled"] is True
    unarchive(api, agent)
    assert api.get(f"/api/triggers/{daily['id']}").json()["enabled"] is False


def test_a_schedule_firing_is_refused_naming_the_agent(api, db):
    agent = make_agent(api, "Newsbot")
    daily = make_trigger(api, schedule("agent", agent["id"]))
    archive(api, agent)
    # Turned back on behind the API's back, it still can't start a run.
    now = datetime.now(UTC)
    db.execute(update(Trigger).where(Trigger.id == daily["id"]).values(enabled=True))
    make_due(db, daily["id"], now - timedelta(seconds=1))

    with use_model(replies("never")):
        tick(now)

    fired = load(db, daily["id"])
    assert fired.last_status == TriggerStatus.failed
    assert fired.last_error == "The agent Newsbot is archived."
    assert fired.last_run_id is None


def test_a_workflow_running_it_at_any_depth_is_refused_naming_the_agent(api, queued):  # noqa: F811
    agent = make_agent(api, "Builder", base_prompt="build")
    direct = make_workflow(api, "Direct", [agent_node("b", agent)])
    middle = make_workflow(api, "Middle", [workflow_node("direct", direct)])
    outer = make_workflow(api, "Outer", [workflow_node("middle", middle)])
    archive(api, agent)

    for workflow in (direct, middle, outer):
        resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
        refused_as_archived(resp, "Builder")
    assert queued == []

    unarchive(api, agent)
    assert api.post(f"/api/workflows/{outer['id']}/runs", json={"input": "x"}).status_code == 201


def test_a_workflow_run_past_its_start_still_runs_the_agents_node(api, queued):  # noqa: F811
    agent = make_agent(api, "Builder", base_prompt="build")
    workflow = make_workflow(api, "Builds", [agent_node("b", agent)])
    started = start(api, workflow, "it")

    archive(api, agent)
    with use_model(echo()):
        asyncio.run(execute_run(started["id"]))

    out = api.get(f"/api/workflows/{workflow['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"]) == ("succeeded", "build(it)")
    assert steps(out)["b"]["status"] == "succeeded"


def test_mentioning_it_posts_the_message_and_fails_at_once_without_a_model_call(api):
    agent = make_agent(api, "Reviewer")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    archive(api, agent)
    calls = []

    async def script(msgs, info):
        calls.append(msgs)
        yield "never"

    with use_model(model(script)), use_handoff(model(script)):
        posted = say(api, chat, f"@[Reviewer](agent:{agent['id']}) look", ("agent", agent))

    assert calls == []
    message, *_ = posted["messages"]
    assert message["body"] == f"@[Reviewer](agent:{agent['id']}) look"
    [invocation] = posted["invocations"]
    assert invocation["status"] == "failed"
    assert invocation["error"] == "The agent Reviewer is archived"
    assert invocation["agent_run_id"] is None
    shown = messages(api, chat)["messages"]
    assert [m["body"] for m in shown if m["kind"] == "text" and m["author"]["kind"] == "agent"] == [
        "Couldn't start the run: The agent Reviewer is archived"
    ]


def test_it_shows_as_archived_among_chat_members_and_isnt_mentionable(api):
    agent = make_agent(api, "Reviewer")
    other = make_agent(api, "Other")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    add(api, chat, "agent", other)

    # Besides the documents assistant every chat starts with.
    def members() -> list[tuple[str, bool]]:
        found = api.get(f"/api/chats/{chat['id']}").json()["members"]["agents"]
        return [(m["name"], m["archived"]) for m in found if m["id"] != found[0]["id"]]

    def mentionable() -> list[str]:
        found = api.get(f"/api/chats/{chat['id']}/mentionable").json()["agents"]
        return [m["name"] for m in found][1:]

    archive(api, agent)
    assert members() == [("Reviewer", True), ("Other", False)]
    assert mentionable() == ["Other"]

    unarchive(api, agent)
    assert members() == [("Reviewer", False), ("Other", False)]
    assert mentionable() == ["Reviewer", "Other"]


def test_used_by_lists_what_uses_it_and_archiving_is_never_refused(
    api,
    queued,  # noqa: F811
):
    agent = make_agent(api, "Builder", base_prompt="build")
    direct = make_workflow(api, "Direct", [agent_node("b", agent)])
    outer = make_workflow(api, "Outer", [workflow_node("direct", direct)])
    make_workflow(api, "Unrelated", [agent_node("o", make_agent(api, "Other"))])
    own = make_trigger(api, schedule("agent", agent["id"]))
    nightly = make_trigger(api, schedule("workflow", outer["id"]))
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    # Runs still going don't hold it up.
    start(api, direct, "x")
    api.post(f"/api/agents/{agent['id']}/runs")

    resp = api.get(f"/api/agents/{agent['id']}/used-by")

    assert resp.status_code == 200, resp.text
    used = resp.json()
    assert [(w["id"], w["name"], w["direct"]) for w in used["workflows"]] == [
        (direct["id"], "Direct", True),
        (outer["id"], "Outer", False),
    ]
    by_workflow = {w["name"]: [t["id"] for t in w["triggers"]] for w in used["workflows"]}
    assert by_workflow == {"Direct": [], "Outer": [nightly["id"]]}
    assert [t["id"] for t in used["triggers"]] == [own["id"]]
    assert [(c["id"], c["name"]) for c in used["chats"]] == [(chat["id"], "Design")]
    assert used["tables"] == []

    # With all of that, archiving still goes through, and the workflows' schedules are left.
    archive(api, agent)
    assert api.get(f"/api/triggers/{nightly['id']}").json()["enabled"] is True


def test_any_member_archives_and_unarchives(api, user, member):  # noqa: F811
    agent = make_agent(api, "Reviewer")

    with signed_in_as(member):
        archived = archive(api, agent)
        assert archived["archived_by"] == {"id": member.id, "name": member.full_name}
        assert unarchive(api, agent)["archived_at"] is None


@pytest.mark.parametrize("path", ["archive", "unarchive", "used-by"])
def test_another_organisations_agent_isnt_found(api, outsider, path):  # noqa: F811
    agent = make_agent(api, "Reviewer")

    with signed_in_as(outsider):
        call = api.get if path == "used-by" else api.post
        assert call(f"/api/agents/{agent['id']}/{path}").status_code == 404
