"""Insights (`aios.insights`, `aios.api.insights`): the panels every member sees, read through
the read-only path from the store's metrics-only `insights` views, by range and grouping, and
cached for a few minutes."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from conftest import join_organisation, store_of, take_turn
from sqlalchemy import delete, func, select, text, update
from test_agents import make_agent
from test_audit_log import outbox_of
from test_chat_agent_invocations import say
from test_chats import add, make_chat, signed_in_as
from test_sources import add_source
from test_usage import MODEL, three_calls
from test_workflows import make_workflow

from aios import insights
from aios.config import get_settings
from aios.datastores import quota
from aios.datastores.models import (
    AgentRun,
    Invocation,
    SchemaChangeRequest,
    Source,
    Turn,
    WorkflowRun,
    WorkflowRunStep,
)
from aios.models import StoreUsageSample
from aios.testing.fake_model import model, use_model


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    """£2 and £10 per million input and output tokens."""
    settings = get_settings()
    monkeypatch.setattr(settings, "price_table", {MODEL: {"input": 2.0, "output": 10.0}})
    monkeypatch.setattr(settings, "llm_model", MODEL)


@pytest.fixture(autouse=True)
def fresh_cache():
    insights.clear_cache()
    yield
    insights.clear_cache()


def panel(api, name: str, **params) -> dict:
    resp = api.get(f"/api/insights/{name}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def by_key(rows: list[dict]) -> dict:
    return {r["key"]: r for r in rows}


def counted_agent(api, name: str = "Counter") -> tuple[dict, dict]:
    """An agent with one turn of three model calls: two describe_data calls, 300 tokens in and
    30 out."""
    agent = make_agent(api, name)
    api.put(
        f"/api/agents/{agent['id']}",
        json={"name": name, "tools": [{"key": "describe_data", "config": {}}]},
    )
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with use_model(three_calls()):
        take_turn(api, agent["id"], run["id"], "go")
    return agent, run


def untracked_run(in_store, agent: dict, run: dict, user) -> int:
    """An agent run from before turns were recorded: messages, but no turns."""
    version = in_store.get(AgentRun, run["id"]).agent_version_id
    old = in_store.add(
        AgentRun(
            agent_id=agent["id"], agent_version_id=version, created_by=user.id, message_count=4
        )
    )
    return old.id


def test_usage_counts_turns_from_before_counts_were_kept_as_not_tracked(api, user, in_store):
    agent, run = counted_agent(api)
    untracked_run(in_store, agent, run, user)

    data = panel(api, "usage", group="agent")

    mine = by_key(data["rows"])[agent["id"]]
    assert mine["name"] == "Counter"
    assert (mine["turns"], mine["not_tracked"]) == (2, 1)
    # Only the tracked turn's tokens: the old one's aren't known, not zero.
    assert (mine["input_tokens"], mine["output_tokens"], mine["tool_calls"]) == (300, 30, 2)
    # An estimate in pounds, from the price table.
    assert data["currency"] == "GBP"
    assert mine["cost"] == pytest.approx(300 * 2 / 1e6 + 30 * 10 / 1e6)
    assert data["top_tools"] == [{"tool": "describe_data", "calls": 2}]
    assert data["top_agents"][0] == {"key": agent["id"], "name": "Counter", "turns": 2}


def seeded_turn(in_store, agent: dict, status: str, user=None, **counters) -> int:
    """A run of the agent with one finished turn, as if it had run."""
    run = in_store.add(
        AgentRun(
            agent_id=agent["id"],
            agent_version_id=agent["current_version"]["id"],
            created_by=user.id if user else None,
            message_count=2,
        )
    )
    turn = in_store.add(
        Turn(
            agent_run_id=run.id,
            agent_version_id=agent["current_version"]["id"],
            status=status,
            finished_at=func.now(),
            **counters,
        )
    )
    return turn.id


def test_activity_counts_turns_by_day_with_how_many_succeeded_and_failed(api, user, in_store):
    agent = make_agent(api, "Helper")
    for status in ("succeeded", "succeeded", "failed"):
        seeded_turn(in_store, agent, status, user)
    old = seeded_turn(in_store, agent, "succeeded", user)
    in_store.execute(delete(Turn).where(Turn.id == old))

    data = panel(api, "activity", group="agent")

    (series,) = data["turns"]
    assert (series["key"], series["name"]) == (agent["id"], "Helper")
    (today,) = series["days"]
    assert today["day"] == datetime.now(UTC).date().isoformat()
    assert (today["total"], today["succeeded"], today["failed"], today["not_tracked"]) == (
        4,
        2,
        1,
        1,
    )
    assert data["workflow_runs"] == []


def test_usage_by_person_is_admins_only_until_an_admin_shows_it_to_everyone(api, db, user):
    editor = join_organisation(db, user, "editor", first_name="Eddie", last_name="Editor")
    with signed_in_as(editor):
        refused = api.get("/api/insights/usage", params={"group": "person"})
        assert refused.status_code == 403
        assert "admins only" in refused.json()["detail"]
        assert api.get("/api/insights/activity", params={"group": "person"}).status_code == 403
        # Every other grouping is everyone's.
        assert api.get("/api/insights/usage", params={"group": "agent"}).status_code == 200
        # Only admins switch it.
        assert (
            api.put("/api/organisation/insights-people", json={"visible": True}).status_code == 403
        )

    resp = api.put("/api/organisation/insights-people", json={"visible": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["insights_people_visible"] is True
    assert [r.details for r in outbox_of(db, user) if r.action == "settings.changed"] == [
        {"setting": "insights_people_visible", "from": False, "to": True}
    ]

    with signed_in_as(editor):
        assert api.get("/api/insights/usage", params={"group": "person"}).status_code == 200


def test_usage_by_person_names_each_person(api, user, in_store):
    agent = make_agent(api, "Helper")
    seeded_turn(in_store, agent, "succeeded", user, input_tokens=10, output_tokens=1)

    data = panel(api, "usage", group="person")

    assert by_key(data["rows"])[user.id]["name"] == "Test User"


def ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def seeded_workflow_run(in_store, workflow: dict, status: str, **values) -> int:
    run = in_store.add(
        WorkflowRun(
            workflow_id=workflow["id"],
            workflow_version_id=workflow["current_version"]["id"],
            input="",
            status=status,
            **values,
        )
    )
    return run.id


def seeded_approval(
    in_store, status: str, created: datetime, decided: datetime | None
) -> uuid.UUID:
    approval = in_store.add(
        SchemaChangeRequest(
            kind="workflow_step",
            reason="Go ahead?",
            approver_role="editor",
            status=status,
            created_at=created,
            decided_at=decided,
        )
    )
    return approval.id


def test_workflow_durations_leave_out_the_time_they_waited_for_approvals(api, in_store):
    workflow = make_workflow(api, "Review")
    approval = seeded_approval(in_store, "approved", ago(90), ago(30))
    run = seeded_workflow_run(
        in_store, workflow, "succeeded", started_at=ago(100), finished_at=ago(0), waited_seconds=60
    )
    in_store.add(
        WorkflowRunStep(
            run_id=run,
            path=["sign_off"],
            kind="approval",
            status="succeeded",
            input="",
            approval_id=approval,
            started_at=ago(95),
            finished_at=ago(25),
        )
    )

    data = panel(api, "workflows")

    (slow,) = data["slowest_workflows"]
    assert (slow["name"], slow["runs"]) == ("Review", 1)
    # 100 seconds, of which 60 waiting.
    assert slow["mean_seconds"] == pytest.approx(40, abs=1)
    (node,) = data["slowest_nodes"]
    assert (node["node"], node["kind"]) == ("sign_off", "approval")
    # 70 seconds, of which 60 waiting for its approval.
    assert node["mean_seconds"] == pytest.approx(10, abs=1)


def test_failures_count_by_node_and_link_to_the_latest_failed_run(api, in_store):
    workflow = make_workflow(api, "Fetch")
    runs = []
    for finished in (300, 100):
        run = seeded_workflow_run(
            in_store, workflow, "failed", started_at=ago(finished + 10), finished_at=ago(finished)
        )
        in_store.add(
            WorkflowRunStep(
                run_id=run,
                path=["fetch"],
                kind="agent",
                status="failed",
                input="",
                error="The site was down.",
                started_at=ago(finished + 10),
                finished_at=ago(finished),
            )
        )
        runs.append(run)

    (failure,) = panel(api, "workflows")["failures"]

    assert (failure["name"], failure["node"], failure["failures"]) == ("Fetch", "fetch", 2)
    assert failure["latest_run_id"] == runs[-1]
    # Metrics only: the error is on the run's page, not here.
    assert "error" not in failure


def test_approvals_count_expired_ones_apart_from_decisions(api, in_store):
    seeded_approval(in_store, "approved", ago(600), ago(500))
    seeded_approval(in_store, "rejected", ago(600), ago(300))
    seeded_approval(in_store, "expired", ago(600), ago(1))
    seeded_approval(in_store, "pending", ago(60), None)

    (kind,) = panel(api, "approvals")["kinds"]

    assert kind["kind"] == "workflow_step"
    assert (kind["total"], kind["approved"], kind["rejected"]) == (4, 1, 1)
    assert (kind["expired"], kind["pending"]) == (1, 1)
    # Waiting times are the decided ones': 100 and 300 seconds.
    assert kind["mean_wait_seconds"] == pytest.approx(200, abs=1)
    assert kind["max_wait_seconds"] == pytest.approx(300, abs=1)


def samples_of(db, store_id: uuid.UUID) -> list[StoreUsageSample]:
    db.expire_all()
    return db.scalars(
        select(StoreUsageSample)
        .where(StoreUsageSample.data_store_id == store_id)
        .order_by(StoreUsageSample.measured_at)
    ).all()


def test_each_measurement_keeps_a_sample_and_old_ones_are_pruned(db, user, monkeypatch):
    store_id = store_of(user)
    quota.measure(store_id)
    quota.measure(store_id)
    first, second = samples_of(db, store_id)
    assert first.db_bytes > 0 and first.files_bytes == 0

    monkeypatch.setattr(get_settings(), "storage_history_days", 400)
    db.execute(
        update(StoreUsageSample)
        .where(StoreUsageSample.id == first.id)
        .values(measured_at=func.now() - timedelta(days=401))
    )
    db.commit()
    quota.prune_samples()

    assert [s.id for s in samples_of(db, store_id)] == [second.id]


def test_storage_shows_usage_against_the_caps_over_time(api, user):
    quota.measure(store_of(user))

    data = panel(api, "storage")

    settings = get_settings()
    assert data["db_quota_bytes"] == settings.data_store_db_quota_bytes
    assert data["files_quota_bytes"] == settings.data_store_files_quota_bytes
    (sample,) = data["samples"]
    assert sample["db_bytes"] == data["db_bytes"] > 0


def test_source_health_is_hidden_with_only_uploads(api):
    assert panel(api, "sources") == {"shown": False, "sources": []}


def test_source_health_matches_the_sources_tab(api, user, in_store):
    source = add_source(store_of(user), "Handbook")
    in_store.execute(update(Source).where(Source.id == source).values(status="failing"))

    data = panel(api, "sources")

    assert data["shown"] is True
    (shown,) = data["sources"]
    tab = {s["id"]: s for s in api.get("/api/sources").json()}[source]
    assert shown == tab
    assert shown["health"] == "failing"


def test_panels_are_cached_for_the_configured_time(api, user, in_store, monkeypatch):
    monkeypatch.setattr(get_settings(), "insights_cache_seconds", 120)
    clock = [1000.0]
    monkeypatch.setattr(insights.time, "monotonic", lambda: clock[0])
    agent = make_agent(api, "Helper")
    seeded_turn(in_store, agent, "succeeded", user)
    assert by_key(panel(api, "usage")["rows"])[agent["id"]]["turns"] == 1

    seeded_turn(in_store, agent, "succeeded", user)
    clock[0] += 119
    assert by_key(panel(api, "usage")["rows"])[agent["id"]]["turns"] == 1
    # Another range or grouping is read afresh.
    assert by_key(panel(api, "usage", group="cause")["rows"])[None]["turns"] == 2

    clock[0] += 2
    assert by_key(panel(api, "usage")["rows"])[agent["id"]]["turns"] == 2


def test_panels_read_only_through_the_read_only_path(api, user, monkeypatch):
    from aios.datastores import connections

    roles, read_only = [], []
    real = connections.store_session

    @contextmanager
    def watched(store_id, role="rw"):
        roles.append(role)
        with real(store_id, role) as session:
            execute = session.execute

            def reading(statement, *args, **kwargs):
                # As each panel query runs: in a read-only transaction?
                if "insights." in str(statement) or "sources" in str(statement):
                    read_only.append(execute(text("SHOW transaction_read_only")).scalar())
                return execute(statement, *args, **kwargs)

            session.execute = reading
            yield session

    monkeypatch.setattr(insights, "store_session", watched)
    add_source(store_of(user), "Handbook")
    for name in ("activity", "usage", "workflows", "approvals", "chats", "sources"):
        panel(api, name)

    assert roles == ["ro"] * 6
    assert read_only and set(read_only) == {"on"}


async def _done(messages, info):
    yield "Done."


def test_chat_activity_counts_messages_mentions_and_how_they_went(api, user, in_store):
    agent = make_agent(api, "Helper")
    busy, quiet = make_chat(api, "Design"), make_chat(api, "Quiet")
    add(api, busy, "agent", agent)
    say(api, busy, "Morning.")
    with use_model(model(_done)):
        say(api, busy, "Please look", ("agent", agent))
        failing = say(api, busy, "And again", ("agent", agent))["invocations"][0]
    in_store.execute(
        update(Invocation).where(Invocation.id == failing["id"]).values(status="failed")
    )
    assert quiet

    (chat,) = panel(api, "chats")["chats"]

    assert (chat["chat_id"], chat["name"], chat["messages"]) == (busy["id"], "Design", 3)
    assert (chat["mentions"], chat["succeeded"], chat["failed"]) == (2, 1, 1)


def test_the_insights_views_hold_no_content(user):
    """Metrics only: no message text, brief, tool input or output, memory, synced text or
    error in any of them."""
    with insights.read_only_session(store_of(user)) as store:
        columns = store.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'insights'"
            )
        ).all()
    views = {t for t, _ in columns}
    assert {"turns", "workflow_runs", "workflow_steps", "usage", "approvals", "chats"} <= views
    content = {"body", "brief", "input", "output", "error", "text", "summary", "content", "reason"}
    assert [(t, c) for t, c in columns if c in content or c.endswith("_json")] == []


@pytest.mark.parametrize(
    "params",
    [{"start": "2026-11-02", "end": "2026-11-01"}, {"start": "2024-01-01", "end": "2026-01-01"}],
)
def test_a_range_must_end_after_it_starts_and_cover_at_most_a_year(api, params):
    assert api.get("/api/insights/usage", params=params).status_code == 422
