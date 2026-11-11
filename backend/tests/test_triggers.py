from datetime import UTC, datetime, timedelta

import pytest
from conftest import delete_user, make_user

from aios.auth import current_user
from aios.cron import next_run
from aios.main import app


def make_agent(api, name: str) -> dict:
    resp = api.post("/api/agents", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_workflow(api, name: str, agent: dict | None = None) -> dict:
    nodes = (
        [{"key": "a", "kind": "agent", "version_id": agent["current_version"]["id"]}]
        if agent
        else []
    )
    resp = api.post("/api/workflows", json={"name": name, "nodes": nodes})
    assert resp.status_code == 201, resp.text
    return resp.json()


def schedule(target_kind: str, target_id: int, **extra) -> dict:
    return {
        "kind": "schedule",
        "target_kind": target_kind,
        "target_id": target_id,
        "cron": "0 9 * * *",
        "timezone": "Europe/London",
        "input": "Summarise yesterday's news.",
        **extra,
    }


def make_trigger(api, body: dict) -> dict:
    resp = api.post("/api/triggers", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def test_create_schedule_for_an_agent(api):
    agent = make_agent(api, "Newsbot")
    before = datetime.now(UTC)
    t = make_trigger(api, schedule("agent", agent["id"], cron="  0  9 * * * "))
    assert t["kind"] == "schedule"
    assert t["target_kind"] == "agent"
    assert t["target_id"] == agent["id"]
    assert t["target_name"] == "Newsbot"
    assert t["enabled"] is True
    assert t["cron"] == "0 9 * * *"
    assert t["description"] == "At 09:00 every day"
    assert t["timezone"] == "Europe/London"
    assert t["input"] == "Summarise yesterday's news."
    assert t["last_fired_at"] is None and t["last_status"] is None and t["last_run_id"] is None
    # The next 09:00 in London after now.
    assert parse(t["next_run_at"]) == next_run("0 9 * * *", "Europe/London", before)

    assert api.get(f"/api/triggers/{t['id']}").json() == t


def test_create_schedule_for_a_workflow(api):
    workflow = make_workflow(api, "Pipeline")
    t = make_trigger(api, schedule("workflow", workflow["id"], input="Today's tickets"))
    assert t["target_kind"] == "workflow"
    assert t["target_name"] == "Pipeline"
    assert t["input"] == "Today's tickets"
    assert t["last_run_id"] is None and t["last_workflow_run_id"] is None


def test_a_workflow_schedule_needs_an_input(api):
    # A workflow run takes a non-blank input, as when it's started by hand.
    workflow = make_workflow(api, "Pipeline")
    for blank in ("", "   "):
        resp = api.post("/api/triggers", json=schedule("workflow", workflow["id"], input=blank))
        assert resp.status_code == 422
        assert "needs an input" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"cron": "61 * * * *"}, "Invalid schedule"),
        ({"cron": ""}, "Invalid schedule"),
        ({"timezone": "Nowhere/Special"}, "Unknown timezone"),
        ({"input": "   "}, "needs a message"),
    ],
)
def test_invalid_schedules_are_rejected(api, changes, detail):
    agent = make_agent(api, "Bot")
    resp = api.post("/api/triggers", json=schedule("agent", agent["id"], **changes))
    assert resp.status_code == 422
    assert detail in resp.json()["detail"]


def test_unknown_kinds_are_rejected(api):
    agent = make_agent(api, "Bot")
    assert (
        api.post("/api/triggers", json=schedule("agent", agent["id"], kind="webhook")).status_code
        == 422
    )
    assert api.post("/api/triggers", json=schedule("skill", agent["id"])).status_code == 422


def test_targets_must_be_the_users_agents_or_workflows_and_not_archived(api, client, db):
    agent = make_agent(api, "Mine")
    workflow = make_workflow(api, "Mine too")
    # A workflow id as an agent, and the other way round.
    missing = max(agent["id"], workflow["id"]) + 1000
    for kind, target in [("agent", missing), ("workflow", missing)]:
        resp = api.post("/api/triggers", json=schedule(kind, target))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Unknown {kind}"
    assert api.post(f"/api/agents/{agent['id']}/archive").status_code == 200
    resp = api.post("/api/triggers", json=schedule("agent", agent["id"]))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The agent Mine is archived."
    # A paused one can be made, or changed, as long as it stays paused.
    paused = make_trigger(api, schedule("agent", agent["id"], enabled=False))
    url = f"/api/triggers/{paused['id']}"
    assert (
        api.put(
            url, json=schedule("agent", agent["id"], enabled=False, cron="0 8 * * *")
        ).status_code
        == 200
    )
    assert api.put(url, json=schedule("agent", agent["id"])).status_code == 409

    other = make_user(db, "other")
    try:
        app.dependency_overrides[current_user] = lambda: other
        resp = client.post("/api/triggers", json=schedule("workflow", workflow["id"]))
        assert resp.status_code == 422
    finally:
        delete_user(db, other)


def test_list_is_upcoming_first_and_filters_by_target(api):
    a = make_agent(api, "A")
    b = make_agent(api, "B")
    now = datetime.now(UTC)
    # Whichever hour comes next fires first; pick hours an hour and two hours ahead.
    soon = (now + timedelta(hours=1)).hour
    later = (now + timedelta(hours=2)).hour
    t_later = make_trigger(api, schedule("agent", a["id"], cron=f"0 {later} * * *", timezone="UTC"))
    t_soon = make_trigger(api, schedule("agent", b["id"], cron=f"0 {soon} * * *", timezone="UTC"))
    t_off = make_trigger(api, schedule("agent", a["id"], enabled=False))
    assert t_off["next_run_at"] is None

    ids = [t["id"] for t in api.get("/api/triggers").json()]
    assert ids == [t_soon["id"], t_later["id"], t_off["id"]]

    by_a = api.get("/api/triggers", params={"target_kind": "agent", "target_id": a["id"]}).json()
    assert [t["id"] for t in by_a] == [t_later["id"], t_off["id"]]


def test_list_filter_needs_both_target_kind_and_id(api):
    agent = make_agent(api, "Bot")
    workflow = make_workflow(api, "Flow")
    make_trigger(api, schedule("agent", agent["id"]))
    make_trigger(api, schedule("workflow", workflow["id"]))
    for params in [
        {"target_kind": "agent"},
        {"target_kind": "workflow"},
        {"target_id": agent["id"]},
    ]:
        resp = api.get("/api/triggers", params=params)
        assert resp.status_code == 422, (params, resp.text)
        assert resp.json()["detail"] == "Filter by both target_kind and target_id, or neither"


def test_update_reschedules(api):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    body = schedule("agent", agent["id"], cron="30 6 * * 1", timezone="UTC", input="Weekly plan")
    before = datetime.now(UTC)
    resp = api.put(f"/api/triggers/{t['id']}", json=body)
    assert resp.status_code == 200, resp.text
    u = resp.json()
    assert u["cron"] == "30 6 * * 1" and u["input"] == "Weekly plan" and u["timezone"] == "UTC"
    assert parse(u["next_run_at"]) == next_run("30 6 * * 1", "UTC", before)

    # Disabling clears the next run; enabling schedules it again.
    off = api.put(f"/api/triggers/{t['id']}", json={**body, "enabled": False}).json()
    assert off["enabled"] is False and off["next_run_at"] is None
    on = api.put(f"/api/triggers/{t['id']}", json={**body, "enabled": True}).json()
    assert on["next_run_at"] is not None

    # The target can be moved to another agent, or a workflow.
    workflow = make_workflow(api, "Flow")
    moved = api.put(f"/api/triggers/{t['id']}", json=schedule("workflow", workflow["id"])).json()
    assert moved["target_kind"] == "workflow" and moved["target_name"] == "Flow"

    bad = api.put(f"/api/triggers/{t['id']}", json={**body, "cron": "nope"})
    assert bad.status_code == 422


def test_delete(api):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    assert api.delete(f"/api/triggers/{t['id']}").status_code == 204
    assert api.get(f"/api/triggers/{t['id']}").status_code == 404
    assert api.delete(f"/api/triggers/{t['id']}").status_code == 404


def test_archiving_the_target_pauses_its_triggers_and_keeps_them(api):
    agent = make_agent(api, "Bot")
    workflow = make_workflow(api, "Flow")
    ta = make_trigger(api, schedule("agent", agent["id"]))
    tw = make_trigger(api, schedule("workflow", workflow["id"]))
    assert api.post(f"/api/agents/{agent['id']}/archive").status_code == 200
    assert api.post(f"/api/workflows/{workflow['id']}/archive").status_code == 200
    for t in (ta, tw):
        shown = api.get(f"/api/triggers/{t['id']}").json()
        assert (shown["enabled"], shown["next_run_at"]) == (False, None)
    assert len(api.get("/api/triggers").json()) == 2


def test_other_users_cannot_see_or_change_triggers(api, client, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    other = make_user(db, "other")
    try:
        app.dependency_overrides[current_user] = lambda: other
        assert client.get("/api/triggers").json() == []
        assert client.get(f"/api/triggers/{t['id']}").status_code == 404
        assert (
            client.put(f"/api/triggers/{t['id']}", json=schedule("agent", agent["id"])).status_code
            == 404
        )
        assert client.delete(f"/api/triggers/{t['id']}").status_code == 404
    finally:
        delete_user(db, other)


def test_preview(api):
    before = datetime.now(UTC)
    resp = api.post("/api/triggers/preview", json={"cron": "@daily", "timezone": "Asia/Tokyo"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "At 00:00 every day"
    runs = [parse(t) for t in body["next_runs"]]
    assert len(runs) == 5
    assert runs[0] == next_run("0 0 * * *", "Asia/Tokyo", before)
    assert all(b - a == timedelta(days=1) for a, b in zip(runs, runs[1:], strict=False))

    bad = api.post("/api/triggers/preview", json={"cron": "* * *", "timezone": "UTC"})
    assert bad.status_code == 422 and "Invalid schedule" in bad.json()["detail"]


def test_timezones(api):
    zones = api.get("/api/triggers/timezones").json()
    assert "UTC" in zones and "Europe/London" in zones and zones == sorted(zones)
