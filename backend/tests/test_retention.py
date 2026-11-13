"""Retention (`aios.retention`): how long an organisation keeps its runs and its chat history,
set by admins on the Organisation page (keep indefinitely by default), and the nightly job that
prunes what's older, in batches, recording each prune in the audit log."""

# ruff: noqa: F811

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import delete_user, join_organisation, store_of
from sqlalchemy import func, select, text, update
from test_agents import make_agent
from test_chats import add, make_chat, messages, post, signed_in_as
from test_data_tools import as_owner, provisioned
from test_search import chat_turn, embedded, found, new_run  # noqa: F401
from test_workflow_runs import queued  # noqa: F401
from test_workflows import agent_node, make_workflow

from aios import checkpoints, retention, worker
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import (
    Invocation,
    SandboxSession,
    SchemaChangeRequest,
    Turn,
    WorkflowRun,
    WorkflowRunStep,
)
from aios.models import AgentRunStatus, InvocationStatus, WorkflowRunStatus
from aios.sandbox import sessions
from aios.testing.fake_model import ToolCall, model, replies, use_handoff, use_model


@pytest.fixture
def member(db, user):
    """An editor of `user`'s organisation."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    delete_user(db, u)


def settings_of(api) -> dict:
    return api.get("/api/organisation").json()["retention"]


def set_retention(api, **days):
    return api.put("/api/organisation/retention", json=days)


# --- Settings --------------------------------------------------------------------------------


def test_both_settings_keep_indefinitely_by_default(api):
    assert settings_of(api) == {"run_days": None, "chat_days": None}


def test_an_admin_sets_and_clears_them_and_each_change_is_audited(api, db, user):
    resp = set_retention(api, run_days=30, chat_days=365)
    assert resp.status_code == 200, resp.text
    assert resp.json()["retention"] == {"run_days": 30, "chat_days": 365}
    assert settings_of(api) == {"run_days": 30, "chat_days": 365}

    assert set_retention(api, run_days=None, chat_days=365).status_code == 200
    assert settings_of(api) == {"run_days": None, "chat_days": 365}

    from test_audit_log import outbox_of

    rows = [r for r in outbox_of(db, user) if r.action == "settings.changed"]
    assert [r.details for r in rows] == [
        {
            "setting": "retention",
            "from": {"run_days": None, "chat_days": None},
            "to": {"run_days": 30, "chat_days": 365},
        },
        {
            "setting": "retention",
            "from": {"run_days": 30, "chat_days": 365},
            "to": {"run_days": None, "chat_days": 365},
        },
    ]


@pytest.mark.parametrize("days", [0, -1, 36_501, 1.5, "soon"])
def test_days_run_from_one_to_a_hundred_years(api, days):
    assert set_retention(api, run_days=days, chat_days=None).status_code == 422
    assert set_retention(api, run_days=None, chat_days=days).status_code == 422
    assert settings_of(api) == {"run_days": None, "chat_days": None}
    assert set_retention(api, run_days=1, chat_days=36_500).status_code == 200


def test_editors_see_them_but_cant_change_them(api, member):
    set_retention(api, run_days=7, chat_days=None)
    with signed_in_as(member):
        assert settings_of(api) == {"run_days": 7, "chat_days": None}
        refused = set_retention(api, run_days=None, chat_days=None)
    assert refused.status_code == 403
    assert "settings" in refused.json()["detail"]
    assert settings_of(api) == {"run_days": 7, "chat_days": None}


# --- Pruning runs ----------------------------------------------------------------------------


DAY = timedelta(days=1)


def age(store_id: uuid.UUID, days: int) -> None:
    """Move everything in the store `days` further into the past, as if it had all happened
    then: what's made after this is newer."""
    shift = {"by": f"{days} days"}
    with store_session(store_id) as store:
        for table, columns in AGED.items():
            sets = ", ".join(f"{c} = {c} - CAST(:by AS interval)" for c in columns)
            store.execute(text(f"UPDATE aios.{table} SET {sets}"), shift)
        store.commit()


AGED = {
    "agent_runs": ("created_at", "updated_at"),
    "turns": ("started_at", "finished_at"),
    "workflow_runs": ("created_at", "started_at", "finished_at"),
    "workflow_run_steps": ("started_at", "finished_at"),
    "chat_messages": ("created_at",),
    "invocations": ("created_at", "started_at", "finished_at"),
    "search_items": ("created_at",),
}


def runs_of(api, agent: dict) -> list[int]:
    return [r["id"] for r in api.get(f"/api/agents/{agent['id']}/runs").json()]


def prune() -> None:
    worker.prune_retention_task()


def audit_entries(api, action: str) -> list[dict]:
    resp = api.get("/api/audit-log", params={"action": action})
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


@pytest.fixture
def store_id(user) -> uuid.UUID:
    return store_of(user)


@pytest.fixture
def sandboxes(monkeypatch) -> list[str]:
    """The sandboxes stopped, by ref, instead of asking a backend to."""
    stopped: list[str] = []

    class Backend:
        async def stop(self, handle):
            stopped.append(handle.ref)

    monkeypatch.setattr(sessions, "get_backend", lambda: Backend())
    return stopped


def give_sandbox(store_id: uuid.UUID, run_id: int, ref: str) -> None:
    with store_session(store_id) as store:
        store.add(SandboxSession(agent_run_id=run_id, ref=ref, endpoint="http://x", secret="s"))
        store.commit()


def test_keeping_runs_indefinitely_prunes_none(api, store_id, embedded):
    baker = make_agent(api, "Baker")
    run = new_run(api, baker)
    chat_turn(api, baker, run, "Keep sourdough alive?")
    age(store_id, 100_000)

    prune()

    assert runs_of(api, baker) == [run["id"]]
    assert audit_entries(api, "runs") == []


def test_finished_runs_older_than_the_setting_go_with_everything_of_theirs(
    api, store_id, embedded, sandboxes
):
    baker = make_agent(api, "Baker")
    old = new_run(api, baker)
    chat_turn(api, baker, old, "How do I keep my sourdough going?", "Feed the starter.")
    give_sandbox(store_id, old["id"], "old-sandbox")
    age(store_id, 31)
    recent = new_run(api, baker)
    chat_turn(api, baker, recent, "And my loaf?", "Bake it hot.")
    set_retention(api, run_days=30, chat_days=None)

    prune()

    assert runs_of(api, baker) == [recent["id"]]
    assert api.get(f"/api/agents/{baker['id']}/runs/{old['id']}").status_code == 404
    # Its turns, history, sandbox and search entries went with it.
    assert checkpoints.history(store_id, old["id"]) == []
    with store_session(store_id) as store:
        assert store.scalar(select(func.count()).where(Turn.agent_run_id == old["id"])) == 0
        assert store.scalar(select(func.count()).select_from(SandboxSession)) == 0
    assert sandboxes == ["old-sandbox"]
    assert found(api, "sourdough", "run_message") == []
    assert [r["text"] for r in found(api, "loaf", "run_message")] == ["And my loaf?"]
    (entry,) = audit_entries(api, "runs")
    assert entry["action"] == "runs.pruned"
    assert entry["actor"]["kind"] == "system"
    assert entry["details"]["count"] == 1
    cutoff = datetime.fromisoformat(entry["details"]["cutoff"])
    assert abs(cutoff - (datetime.now(UTC) - 30 * DAY)) < timedelta(minutes=5)


def test_a_run_used_again_lately_is_kept(api, store_id, embedded):
    baker = make_agent(api, "Baker")
    run = new_run(api, baker)
    chat_turn(api, baker, run, "Proving times?")
    age(store_id, 60)
    chat_turn(api, baker, run, "And again?")
    set_retention(api, run_days=30, chat_days=None)

    prune()

    assert runs_of(api, baker) == [run["id"]]


def test_runs_in_flight_or_waiting_on_an_approval_are_never_pruned(api, store_id, embedded):
    baker = make_agent(api, "Baker")
    running, waiting = new_run(api, baker), new_run(api, baker)
    for run in (running, waiting):
        chat_turn(api, baker, run, "Hello")
    with store_session(store_id) as store:
        store.execute(
            update(Turn)
            .where(Turn.agent_run_id == running["id"])
            .values(status=AgentRunStatus.running, finished_at=None)
        )
        store.add(
            SchemaChangeRequest(
                ops=[],
                sql="SELECT 1",
                summary="Create table data.contacts",
                reason="For people",
                requested_by_run=waiting["id"],
                tool_call_id="call_1",
                base_revision=0,
            )
        )
        store.commit()
    age(store_id, 400)
    set_retention(api, run_days=1, chat_days=None)

    prune()

    assert sorted(runs_of(api, baker)) == sorted([running["id"], waiting["id"]])
    assert audit_entries(api, "runs")[0]["details"]["count"] == 0


def test_finished_workflow_runs_go_with_their_steps_and_agent_runs_but_live_ones_stay(
    api, store_id, queued
):
    from test_workflow_runs import agents, run, start

    a = agents(api, "one")
    wf = make_workflow(api, "flow", [agent_node("one", a["one"])])
    done = run(api, queued, wf)
    (node_run,) = [s["agent_run_id"] for s in done["steps"]]
    live = {status: start(api, wf) for status in ("queued", "running", "waiting")}
    with store_session(store_id) as store:
        for status, started in live.items():
            store.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == started["id"])
                .values(status=WorkflowRunStatus(status))
            )
        store.commit()
    age(store_id, 10)
    set_retention(api, run_days=7, chat_days=None)

    prune()

    remaining = [r["id"] for r in api.get(f"/api/workflows/{wf['id']}/runs").json()]
    assert sorted(remaining) == sorted(r["id"] for r in live.values())
    assert runs_of(api, a["one"]) == []
    assert checkpoints.history(store_id, node_run) == []
    with store_session(store_id) as store:
        assert store.scalar(select(func.count()).where(WorkflowRunStep.run_id == done["id"])) == 0
    (entry,) = audit_entries(api, "runs")
    assert entry["details"]["count"] == 1


def test_switching_back_to_keep_indefinitely_stops_pruning(api, store_id, embedded):
    baker = make_agent(api, "Baker")
    first = new_run(api, baker)
    chat_turn(api, baker, first, "One")
    age(store_id, 40)
    set_retention(api, run_days=30, chat_days=None)
    prune()
    assert runs_of(api, baker) == []

    second = new_run(api, baker)
    chat_turn(api, baker, second, "Two")
    age(store_id, 40)
    set_retention(api, run_days=None, chat_days=None)
    prune()

    assert runs_of(api, baker) == [second["id"]]
    assert len(audit_entries(api, "runs")) == 1


def test_runs_are_pruned_in_batches(api, store_id, embedded, monkeypatch):
    monkeypatch.setattr(retention, "BATCH", 2)
    baker = make_agent(api, "Baker")
    for _ in range(5):
        chat_turn(api, baker, new_run(api, baker), "Hi")
    age(store_id, 40)
    set_retention(api, run_days=30, chat_days=None)
    batches: list[int] = []
    deleting = retention._delete_runs
    monkeypatch.setattr(
        retention,
        "_delete_runs",
        lambda store, ids: (batches.append(len(ids)), deleting(store, ids))[1],
    )

    prune()

    assert runs_of(api, baker) == []
    assert batches == [2, 2, 1]
    assert audit_entries(api, "runs")[0]["details"]["count"] == 5


def test_the_job_runs_nightly_on_the_maintenance_beat():
    (entry,) = [
        e
        for e in worker.celery_app.conf.beat_schedule.values()
        if e["task"] == "aios.prune_retention"
    ]
    schedule = entry["schedule"]
    assert schedule.hour == {get_settings().retention_prune_hour}
    assert schedule.minute == {0}


# --- Pruning chat history --------------------------------------------------------------------


def bodies(api, chat: dict) -> list[str]:
    return [m["body"] for m in messages(api, chat)["messages"]]


def mention(api, chat: dict, agent: dict, body: str, brief: str) -> dict:
    """Post `body` mentioning `agent`, whose handoff briefs it with `brief` and which replies;
    answers what the post answered."""

    async def handoff(msgs, info):
        yield ToolCall("Handoff", {"ack": "On it.", "brief": brief})

    with use_handoff(model(handoff)), use_model(replies("Done.")):
        resp = api.post(
            f"/api/chats/{chat['id']}/messages",
            json={"body": body, "mentions": [{"kind": "agent", "id": agent["id"]}]},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_keeping_chats_indefinitely_prunes_no_message(api, store_id, embedded):
    chat = make_chat(api, "Garden")
    post(api, chat, "Plant the beans")
    age(store_id, 100_000)
    set_retention(api, run_days=1, chat_days=None)

    prune()

    assert bodies(api, chat) == ["Plant the beans"]
    assert audit_entries(api, "messages") == []


def test_old_messages_go_with_their_mentions_invocations_briefs_and_search_entries(
    api, store_id, embedded
):
    chat = make_chat(api, "Garden")
    helper = make_agent(api, "Helper")
    add(api, chat, "agent", helper)
    post(api, chat, "The kittens sleep in the shed")
    mention(api, chat, helper, "Feed the kittens", "Kittens eat at six.")
    old_run = runs_of(api, helper)
    # The plain message, the mention, and its run card (the result is in it).
    old_count = len(messages(api, chat)["messages"])
    age(store_id, 8)
    post(api, chat, "Water the sourdough starter")
    set_retention(api, run_days=None, chat_days=7)

    prune()

    # The chat stays, with what's newer.
    assert api.get(f"/api/chats/{chat['id']}").status_code == 200
    assert bodies(api, chat) == ["Water the sourdough starter"]
    assert found(api, "kittens", "chat_message", "brief") == []
    assert [r["text"] for r in found(api, "sourdough", "chat_message")] == [
        "Water the sourdough starter"
    ]
    with store_session(store_id) as store:
        assert store.scalar(select(func.count()).select_from(Invocation)) == 0
    # Runs follow their own setting: the mention's run is kept.
    assert runs_of(api, helper) == old_run
    (entry,) = audit_entries(api, "messages")
    assert entry["action"] == "messages.pruned"
    assert entry["details"]["count"] == old_count


def test_a_message_whose_invocation_is_unfinished_is_kept_until_it_finishes(
    api, store_id, embedded
):
    chat = make_chat(api, "Garden")
    helper = make_agent(api, "Helper")
    add(api, chat, "agent", helper)
    posted = mention(api, chat, helper, "Feed the kittens", "Kittens eat at six.")
    (invocation,) = posted["invocations"]
    with store_session(store_id) as store:
        store.execute(
            update(Invocation)
            .where(Invocation.id == invocation["id"])
            .values(status=InvocationStatus.running, finished_at=None)
        )
        store.commit()
    age(store_id, 30)
    set_retention(api, run_days=None, chat_days=7)

    prune()

    kept = bodies(api, chat)
    assert "Feed the kittens" in kept
    assert audit_entries(api, "messages")[0]["details"]["count"] == 0

    with store_session(store_id) as store:
        store.execute(
            update(Invocation)
            .where(Invocation.id == invocation["id"])
            .values(
                status=InvocationStatus.succeeded,
                finished_at=func.now() - timedelta(days=20),
            )
        )
        store.commit()
    prune()

    assert bodies(api, chat) == []
    assert api.get(f"/api/chats/{chat['id']}").status_code == 200


def test_a_mention_whose_run_finished_after_the_cutoff_is_kept(api, store_id, embedded):
    chat = make_chat(api, "Garden")
    helper = make_agent(api, "Helper")
    add(api, chat, "agent", helper)
    posted = mention(api, chat, helper, "Feed the kittens", "Kittens eat at six.")
    (invocation,) = posted["invocations"]
    age(store_id, 30)
    with store_session(store_id) as store:
        store.execute(
            update(Invocation)
            .where(Invocation.id == invocation["id"])
            .values(finished_at=func.now() - timedelta(days=1))
        )
        store.commit()
    set_retention(api, run_days=None, chat_days=7)

    prune()

    assert "Feed the kittens" in bodies(api, chat)


def test_documents_data_tables_usage_and_the_audit_log_are_never_pruned(
    api, db, user, store_id, embedded, files_bucket
):
    from conftest import add_document

    doc = add_document(store_id, b"# Beans")
    with as_owner(provisioned(db, store_id)) as conn:
        conn.execute("CREATE TABLE data.beans (id int)")
        conn.execute("INSERT INTO data.beans VALUES (1)")
    set_retention(api, run_days=1, chat_days=1)
    audit_before = api.get("/api/audit-log").json()["entries"]
    age(store_id, 100_000)
    with store_session(store_id) as store:
        store.execute(
            text("UPDATE aios.documents SET created_at = created_at - interval '300 years'")
        )
        store.commit()

    prune()
    prune()

    assert [d["id"] for d in api.get("/api/documents").json()] == [doc]
    with store_session(store_id) as store:
        assert store.scalar(text("SELECT count(*) FROM data.beans")) == 1
    audit_after = api.get("/api/audit-log").json()["entries"]
    assert {e["id"] for e in audit_before} <= {e["id"] for e in audit_after}
    assert [e["action"] for e in audit_after[:6]] == [
        "messages.pruned",
        "turns.pruned",
        "runs.pruned",
        "messages.pruned",
        "turns.pruned",
        "runs.pruned",
    ]
