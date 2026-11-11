"""Archiving a workflow, on the same terms as an agent: nothing is lost, and nothing new starts
on it.

`POST /api/workflows/{id}/archive` and `/unarchive` put it away and bring it back, never
refused. `GET /api/workflows/{id}/used-by` is what the archive confirmation shows. An archived
workflow starts no new run by hand, by schedule, by mention or nested in a live workflow, while
runs already going finish; its schedules pause and stay paused after unarchiving.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from test_chat_invocations import say
from test_chats import add, make_chat, messages, signed_in_as
from test_organisation_scoping import member, outsider  # noqa: F401 (fixtures)
from test_scheduler import load, make_due, tick
from test_triggers import make_trigger, schedule
from test_workflow_runs import echo, queued, start, steps  # noqa: F401 (a fixture)
from test_workflows import agent_node, make_agent, make_workflow, workflow_node

from aios.models import Trigger, TriggerStatus
from aios.testing.fake_model import model, use_handoff, use_model
from aios.workflows import execute_run


def archive(api, workflow: dict) -> dict:
    resp = api.post(f"/api/workflows/{workflow['id']}/archive")
    assert resp.status_code == 200, resp.text
    return resp.json()


def unarchive(api, workflow: dict) -> dict:
    resp = api.post(f"/api/workflows/{workflow['id']}/unarchive")
    assert resp.status_code == 200, resp.text
    return resp.json()


def refused_as_archived(resp, name: str) -> None:
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == f"The workflow {name} is archived."


def builds(api, name: str = "Builds") -> dict:
    return make_workflow(api, name, [agent_node("b", make_agent(api, f"{name} agent", "build"))])


def test_archiving_and_unarchiving_a_workflow(api, user, queued):  # noqa: F811
    workflow = builds(api)
    assert (workflow["archived_at"], workflow["archived_by"]) == (None, None)

    archived = archive(api, workflow)

    assert archived["archived_at"] is not None
    assert archived["archived_by"] == {"id": user.id, "name": user.full_name}
    # Its page still opens, archived, with its versions and runs.
    shown = api.get(f"/api/workflows/{workflow['id']}").json()
    assert shown["archived_at"] == archived["archived_at"]
    assert api.get(f"/api/workflows/{workflow['id']}/versions").status_code == 200
    assert api.get(f"/api/workflows/{workflow['id']}/runs").status_code == 200
    # Archiving again changes nothing, and is never refused.
    assert archive(api, workflow)["archived_at"] == archived["archived_at"]

    back = unarchive(api, workflow)

    assert (back["archived_at"], back["archived_by"]) == (None, None)
    assert start(api, back, "x")["status"] == "queued"


def test_the_workflows_list_hides_archived_workflows_unless_asked_for_them(api):
    kept = make_workflow(api, "Kept")
    put_away = make_workflow(api, "Put away")
    archive(api, put_away)

    default = api.get("/api/workflows").json()
    archived = api.get("/api/workflows", params={"archived": "true"}).json()

    assert [w["name"] for w in default] == ["Kept"]
    assert default[0]["archived_at"] is None
    assert [w["name"] for w in archived] == ["Put away"]
    assert archived[0]["archived_at"] is not None
    unarchive(api, put_away)
    assert [w["id"] for w in api.get("/api/workflows").json()] == [kept["id"], put_away["id"]]


def test_an_archived_workflows_name_stays_reserved(api):
    workflow = make_workflow(api, "Digest")
    archive(api, workflow)

    assert api.post("/api/workflows", json={"name": "Digest"}).status_code == 409
    assert unarchive(api, workflow)["name"] == "Digest"


def test_a_run_by_hand_is_refused_naming_the_workflow(api, queued):  # noqa: F811
    workflow = builds(api)
    archive(api, workflow)

    resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})

    refused_as_archived(resp, "Builds")
    assert queued == []


def test_a_live_workflow_nesting_it_at_any_depth_is_refused_naming_it(api, queued):  # noqa: F811
    inner = builds(api, "Inner")
    middle = make_workflow(api, "Middle", [workflow_node("inner", inner)])
    outer = make_workflow(api, "Outer", [workflow_node("middle", middle)])
    archive(api, inner)

    for workflow in (middle, outer):
        resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
        refused_as_archived(resp, "Inner")
    assert queued == []

    unarchive(api, inner)
    assert api.post(f"/api/workflows/{outer['id']}/runs", json={"input": "x"}).status_code == 201


def test_a_run_already_going_finishes(api, queued):  # noqa: F811
    workflow = builds(api)
    started = start(api, workflow, "it")

    archive(api, workflow)
    with use_model(echo()):
        asyncio.run(execute_run(started["id"]))

    out = api.get(f"/api/workflows/{workflow['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"]) == ("succeeded", "build(it)")
    assert steps(out)["b"]["status"] == "succeeded"


def test_its_schedules_pause_and_stay_paused_after_unarchiving(api):
    workflow = builds(api)
    nightly = make_trigger(api, schedule("workflow", workflow["id"]))
    other = make_trigger(api, schedule("workflow", builds(api, "Other")["id"]))

    archive(api, workflow)

    assert api.get(f"/api/triggers/{nightly['id']}").json()["enabled"] is False
    assert api.get(f"/api/triggers/{other['id']}").json()["enabled"] is True
    unarchive(api, workflow)
    assert api.get(f"/api/triggers/{nightly['id']}").json()["enabled"] is False


def test_a_schedule_firing_is_refused_naming_the_workflow(api, db, queued):  # noqa: F811
    workflow = builds(api)
    nightly = make_trigger(api, schedule("workflow", workflow["id"]))
    archive(api, workflow)
    # Turned back on behind the API's back, it still can't start a run.
    now = datetime.now(UTC)
    db.execute(update(Trigger).where(Trigger.id == nightly["id"]).values(enabled=True))
    make_due(db, nightly["id"], now - timedelta(seconds=1))

    tick(now)

    fired = load(db, nightly["id"])
    assert fired.last_status == TriggerStatus.failed
    assert fired.last_error == "The workflow Builds is archived."
    assert fired.last_workflow_run_id is None
    assert queued == []


def test_a_schedule_for_an_archived_workflow_is_refused(api):
    workflow = builds(api)
    archive(api, workflow)

    resp = api.post("/api/triggers", json=schedule("workflow", workflow["id"]))

    refused_as_archived(resp, "Builds")


def test_mentioning_it_posts_the_message_and_fails_at_once_without_a_model_call(api, queued):  # noqa: F811
    workflow = builds(api, "Digest")
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", workflow)
    archive(api, workflow)
    calls = []

    async def script(msgs, info):
        calls.append(msgs)
        yield "never"

    with use_model(model(script)), use_handoff(model(script)):
        posted = say(api, chat, f"@[Digest](workflow:{workflow['id']}) go", workflow)

    assert calls == []
    assert queued == []
    [invocation] = posted["invocations"]
    assert invocation["status"] == "failed"
    assert invocation["error"] == "The workflow Digest is archived"
    shown = messages(api, chat)["messages"]
    assert [
        m["body"] for m in shown if m["kind"] == "text" and m["author"]["kind"] == "workflow"
    ] == ["Couldn't start the run: The workflow Digest is archived"]


def test_it_shows_as_archived_among_chat_members_and_isnt_mentionable(api):
    workflow = builds(api, "Digest")
    other = builds(api, "Other")
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", workflow)
    add(api, chat, "workflow", other)

    def members() -> list[tuple[str, bool]]:
        found = api.get(f"/api/chats/{chat['id']}").json()["members"]["workflows"]
        return [(m["name"], m["archived"]) for m in found]

    def mentionable() -> list[str]:
        return [
            m["name"] for m in api.get(f"/api/chats/{chat['id']}/mentionable").json()["workflows"]
        ]

    archive(api, workflow)
    assert members() == [("Digest", True), ("Other", False)]
    assert mentionable() == ["Other"]

    unarchive(api, workflow)
    assert members() == [("Digest", False), ("Other", False)]
    assert mentionable() == ["Digest", "Other"]


def test_used_by_lists_nesting_workflows_schedules_and_chats_and_archiving_is_never_refused(
    api,
    queued,  # noqa: F811
):
    inner = builds(api, "Inner")
    middle = make_workflow(api, "Middle", [workflow_node("inner", inner)])
    outer = make_workflow(api, "Outer", [workflow_node("middle", middle)])
    builds(api, "Unrelated")
    own = make_trigger(api, schedule("workflow", inner["id"]))
    nightly = make_trigger(api, schedule("workflow", outer["id"]))
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", inner)
    start(api, inner, "x")  # a run still going

    resp = api.get(f"/api/workflows/{inner['id']}/used-by")

    assert resp.status_code == 200, resp.text
    used = resp.json()
    assert [(w["name"], w["direct"]) for w in used["workflows"]] == [
        ("Middle", True),
        ("Outer", False),
    ]
    by_workflow = {w["name"]: [t["id"] for t in w["triggers"]] for w in used["workflows"]}
    assert by_workflow == {"Middle": [], "Outer": [nightly["id"]]}
    assert [t["id"] for t in used["triggers"]] == [own["id"]]
    assert [(c["id"], c["name"]) for c in used["chats"]] == [(chat["id"], "Design")]

    # With all of that, archiving still goes through, and the nesting workflows' schedules are
    # left alone.
    archive(api, inner)
    assert api.get(f"/api/triggers/{nightly['id']}").json()["enabled"] is True


def test_any_member_archives_and_unarchives(api, member):  # noqa: F811
    workflow = make_workflow(api, "Digest")

    with signed_in_as(member):
        archived = archive(api, workflow)
        assert archived["archived_by"] == {"id": member.id, "name": member.full_name}
        assert unarchive(api, workflow)["archived_at"] is None


@pytest.mark.parametrize("path", ["archive", "unarchive", "used-by"])
def test_another_organisations_workflow_isnt_found(api, outsider, path):  # noqa: F811
    workflow = make_workflow(api, "Digest")

    with signed_in_as(outsider):
        call = api.get if path == "used-by" else api.post
        assert call(f"/api/workflows/{workflow['id']}/{path}").status_code == 404


def test_no_route_deletes_a_workflow(api):
    workflow = make_workflow(api, "Digest")

    assert api.delete(f"/api/workflows/{workflow['id']}").status_code == 405
    assert api.get(f"/api/workflows/{workflow['id']}").status_code == 200
