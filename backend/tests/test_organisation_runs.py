"""Runs execute for the organisation: however a run starts (by hand, by a schedule or by a chat
mention), its tools use the organisation's store, it records which member started it (none for a
schedule's), its trace carries both, and every member reads it while nobody outside does. A run
doesn't depend on the member who made its agent: it still starts once they've left."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F401, F811

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    delete_user,
    drain_run,
    join_organisation,
    make_user,
    provisioned_user,
    store_of,
    take_turn,
)
from langchain_core.callbacks import BaseCallbackHandler
from test_agents import agent_body, make_agent
from test_chats import add, make_chat, messages, signed_in_as
from test_scheduler import make_due, tick
from test_search import add_chunks, unit
from test_triggers import make_trigger, schedule
from test_workflow_runs import queued
from test_workflows import agent_node, make_workflow, workflow_node

from aios import search, tracing
from aios.testing.fake_model import ToolCall, last_tool_results, model, tool_output, use_model

SEARCH = {"key": "hybrid_search", "config": {}}


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation, who isn't its creator."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    delete_user(db, u)


@pytest.fixture
def outsider(db):
    """A user in an organisation of their own, with its store provisioned."""
    u = provisioned_user(db, "outsider")
    yield u
    delete_user(db, u)


def person(u) -> dict:
    return {"id": u.id, "name": u.full_name, "former_member": False}


def ok(resp, code: int = 200) -> dict:
    assert resp.status_code == code, resp.text
    return resp.json() if code != 204 else {}


@pytest.fixture
def shared_document(user, member, monkeypatch):
    """A document `member` uploaded to the organisation's store, which any query finds."""
    add_chunks(store_of(member), "minutes.md", [("The board met on Tuesday.", unit(0))])

    async def embed_query(text):
        return unit(0)

    monkeypatch.setattr(search, "embed_query", embed_query)


def searches() -> tuple:
    """A model whose agents search the documents once, then reply; and the sources each search
    found, in order."""
    found: list[list[str]] = []

    async def script(messages, info):
        if returns := last_tool_results(messages):
            found.append([r["source"] for r in tool_output(returns[0])])
            yield "Found it."
        else:
            yield ToolCall("search_documents", {"query": "board"})

    return model(script), found


def librarian(api, name: str = "Librarian") -> dict:
    """An agent that searches the organisation's documents."""
    agent = make_agent(api, name)
    return ok(api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[SEARCH])))


def mention_agent(api, chat: dict, agent: dict, body: str = "Find the minutes") -> dict:
    """Mention the agent in the chat; its turn runs before this returns. The invocation."""
    ok(
        api.post(
            f"/api/chats/{chat['id']}/messages",
            json={"body": body, "mentions": [{"kind": "agent", "id": agent["id"]}]},
        ),
        201,
    )
    return messages(api, chat)["invocations"][-1]


def mention_workflow(api, chat: dict, workflow: dict, queued: list[int]) -> int:
    """Mention the workflow in the chat and run what it started, as the worker would."""
    ok(
        api.post(
            f"/api/chats/{chat['id']}/messages",
            json={"body": "Go", "mentions": [{"kind": "workflow", "id": workflow["id"]}]},
        ),
        201,
    )
    drain_run(queued.store, queued[-1])
    return queued[-1]


def fire(api, db, target_kind: str, target: dict) -> dict:
    """Make a schedule of the target and fire it now; the schedule as it then is."""
    t = make_trigger(api, schedule(target_kind, target["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    assert tick(now) == [t["id"]]
    return ok(api.get(f"/api/triggers/{t['id']}"))


def agent_run(api, agent: dict, run_id: int) -> dict:
    return ok(api.get(f"/api/agents/{agent['id']}/runs/{run_id}"))


def listed(api, agent: dict) -> dict[int, dict]:
    return {r["id"]: r for r in ok(api.get(f"/api/agents/{agent['id']}/runs"))}


# --- The store, and who started it, per way a run starts ----------------------------------------


def test_a_run_started_by_hand_searches_the_organisations_store_and_says_who_started_it(
    api, user, member, shared_document
):
    agent = librarian(api)
    fake, found = searches()
    run = ok(api.post(f"/api/agents/{agent['id']}/runs"), 201)
    assert run["created_by"] == person(user)
    with use_model(fake):
        take_turn(api, agent["id"], run["id"], "find the minutes")
    # The member's document, in the store the run's tools were given.
    assert found == [["minutes.md"]]
    assert agent_run(api, agent, run["id"])["created_by"] == person(user)
    assert listed(api, agent)[run["id"]]["created_by"] == person(user)

    # A run another member starts is theirs, of the same store.
    with signed_in_as(member):
        theirs = ok(api.post(f"/api/agents/{agent['id']}/runs"), 201)
        with use_model(fake):
            take_turn(api, agent["id"], theirs["id"], "and again")
    assert found == [["minutes.md"], ["minutes.md"]]
    assert listed(api, agent)[theirs["id"]]["created_by"] == person(member)


def test_a_scheduled_run_searches_the_organisations_store_and_was_started_by_no_one(
    api, db, shared_document
):
    agent = librarian(api)
    fake, found = searches()
    with use_model(fake):
        fired = fire(api, db, "agent", agent)
    assert fired["last_status"] == "succeeded", fired
    assert found == [["minutes.md"]]
    run = agent_run(api, agent, fired["last_run_id"])
    assert (run["created_by"], run["trigger_id"]) == (None, fired["id"])
    assert listed(api, agent)[run["id"]]["created_by"] is None


def test_a_mentions_run_searches_the_organisations_store_and_was_started_by_who_asked(
    api, user, member, shared_document
):
    agent = librarian(api)
    chat = make_chat(api, "Board")
    add(api, chat, "agent", agent)
    fake, found = searches()
    with signed_in_as(member):
        ok(api.post(f"/api/chats/{chat['id']}/join"))
        with use_model(fake):
            invocation = mention_agent(api, chat, agent)
    assert invocation["status"] == "succeeded", invocation
    assert found == [["minutes.md"]]
    run = agent_run(api, agent, invocation["agent_run_id"])
    assert (run["created_by"], run["chat_id"]) == (person(member), chat["id"])


def test_workflow_runs_search_the_organisations_store_and_say_who_started_them(
    api, db, user, member, shared_document, queued
):
    agent = librarian(api)
    wf = make_workflow(api, "Minutes", [agent_node("find", agent)])
    runs = f"/api/workflows/{wf['id']}/runs"
    fake, found = searches()

    with use_model(fake):
        with signed_in_as(member):
            by_hand = ok(api.post(runs, json={"input": "find them"}), 201)
        assert by_hand["created_by"] == person(member)
        drain_run(queued.store, by_hand["id"])

        chat = make_chat(api, "Board")
        add(api, chat, "workflow", wf)
        mentioned = mention_workflow(api, chat, wf, queued)

        fired = fire(api, db, "workflow", wf)
        scheduled = fired["last_workflow_run_id"]
        drain_run(queued.store, scheduled)

    assert found == [["minutes.md"]] * 3
    expected = {by_hand["id"]: person(member), mentioned: person(user), scheduled: None}
    listing = {r["id"]: r for r in ok(api.get(runs))}
    assert {i: r["created_by"] for i, r in listing.items()} == expected
    assert listing[scheduled]["trigger_id"] == fired["id"]
    assert listing[by_hand["id"]]["trigger_id"] is None
    for run_id, starter in expected.items():
        page = ok(api.get(f"{runs}/{run_id}"))
        assert (page["status"], page["created_by"]) == ("succeeded", starter)
        # Its agent node's conversation was started by whoever started the run.
        [step] = page["steps"]
        assert agent_run(api, agent, step["agent_run_id"])["created_by"] == starter


# --- Reading ------------------------------------------------------------------------------------


def test_any_member_reads_every_run_in_the_organisation_and_nobody_outside_it_does(
    api, db, user, member, outsider, queued
):
    agent = make_agent(api, "Bot")
    inner = make_workflow(api, "Inner", [agent_node("leaf", agent)])
    outer = make_workflow(api, "Outer", [workflow_node("in", inner)])
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    add(api, chat, "workflow", outer)

    fake, _ = searches()
    with use_model(fake):
        by_hand = ok(api.post(f"/api/agents/{agent['id']}/runs"), 201)["id"]
        mentioned = mention_agent(api, chat, agent)["agent_run_id"]
        scheduled = fire(api, db, "agent", agent)["last_run_id"]
        wf_run = mention_workflow(api, chat, outer, queued)
    wf_url = f"/api/workflows/{outer['id']}/runs/{wf_run}"
    [_, leaf] = ok(api.get(wf_url))["steps"]
    urls = [
        *(f"/api/agents/{agent['id']}/runs/{i}" for i in (by_hand, mentioned, scheduled)),
        f"/api/agents/{leaf['agent_id']}/runs/{leaf['agent_run_id']}",
        wf_url,
        f"{wf_url}/subworkflow?path=in",
    ]

    mine = [ok(api.get(u)) for u in urls]
    # Nothing on a run says whether it's the reader's: every member's is the same.
    assert all("is_owner" not in r for r in mine)
    with signed_in_as(member):
        # Not in the private chat that started two of them, and reading them all the same.
        assert [ok(api.get(u)) for u in urls] == mine
        assert set(listed(api, agent)) == {by_hand, mentioned, scheduled, leaf["agent_run_id"]}
    with signed_in_as(outsider):
        assert [api.get(u).status_code for u in urls] == [404] * len(urls)
        assert api.get(f"/api/agents/{agent['id']}/runs").status_code == 404


def test_a_run_isnt_read_through_another_agent_or_once_its_gone(api, user, member):
    agent, other = make_agent(api, "Bot"), make_agent(api, "Other")
    run = ok(api.post(f"/api/agents/{agent['id']}/runs"), 201)["id"]
    with signed_in_as(member):
        assert api.get(f"/api/agents/{other['id']}/runs/{run}").status_code == 404
        ok(api.delete(f"/api/agents/{agent['id']}/runs/{run}"), 204)
        assert api.get(f"/api/agents/{agent['id']}/runs/{run}").status_code == 404


# --- After the agent's creator has left ----------------------------------------------------------


def test_a_schedule_and_a_mention_still_run_an_agent_whose_creator_left(
    api, db, user, member, shared_document
):
    with signed_in_as(member):
        agent = librarian(api, "Theirs")
        t = make_trigger(api, schedule("agent", agent["id"]))
        ok(api.post("/api/organisation/leave"))
    chat = make_chat(api, "Board")
    add(api, chat, "agent", agent)
    fake, found = searches()
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with use_model(fake):
        assert tick(now) == [t["id"]]
        invocation = mention_agent(api, chat, agent)

    fired = ok(api.get(f"/api/triggers/{t['id']}"))
    assert (fired["last_status"], invocation["status"]) == ("succeeded", "succeeded")
    # Over the organisation's store, which the creator no longer reaches.
    assert found == [["minutes.md"], ["minutes.md"]]
    assert agent_run(api, agent, fired["last_run_id"])["created_by"] is None
    assert agent_run(api, agent, invocation["agent_run_id"])["created_by"] == person(user)
    with signed_in_as(member):
        assert api.get(f"/api/agents/{agent['id']}/runs/{fired['last_run_id']}").status_code == 404


# --- Traces --------------------------------------------------------------------------------------


class Traced(BaseCallbackHandler):
    """Notes the metadata of each chat model call it's told of."""

    def __init__(self):
        self.calls: list[dict] = []

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
        self.calls.append(metadata or {})


def who(metadata: dict) -> tuple:
    return (
        metadata.get("organisation_id"),
        metadata.get("created_by"),
        metadata.get("langfuse_user_id"),
    )


def test_traces_carry_the_organisation_and_who_started_the_run(
    api, db, user, member, queued, monkeypatch
):
    from conftest import org_of

    traced = Traced()
    monkeypatch.setattr(tracing, "callbacks", lambda: [traced])
    org = org_of(db, user).id
    agent = make_agent(api, "Bot")
    wf = make_workflow(api, "Flow", [agent_node("a", agent)])
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    fake, _ = searches()

    def seen(tag: str, **where) -> set[tuple]:
        """Who each traced model call tagged `tag` (and matching `where`) says it's for."""
        return {
            who(m)
            for m in traced.calls
            if tag in m.get("langfuse_tags", []) and all(m.get(k) == v for k, v in where.items())
        }

    with use_model(fake):
        with signed_in_as(member):
            run = ok(api.post(f"/api/agents/{agent['id']}/runs"), 201)
            take_turn(api, agent["id"], run["id"], "hi")
        assert seen("agent:Bot", agent_run_id=run["id"]) == {(org, member.id, str(member.id))}

        fired = fire(api, db, "agent", agent)
        # A schedule's run is the organisation's, and no member's.
        assert seen("agent:Bot", agent_run_id=fired["last_run_id"]) == {(org, None, None)}

        with signed_in_as(member):
            ok(api.post(f"/api/chats/{chat['id']}/join"))
            invocation = mention_agent(api, chat, agent)
        assert seen("handoff") == {(org, member.id, str(member.id))}
        assert seen("agent:Bot", agent_run_id=invocation["agent_run_id"]) == {
            (org, member.id, str(member.id))
        }

        started = ok(api.post(f"/api/workflows/{wf['id']}/runs", json={"input": "x"}), 201)
        drain_run(queued.store, started["id"])
        assert seen("workflow:Flow", workflow_run_id=started["id"]) == {
            (org, user.id, str(user.id))
        }

    # Each run's trace session is named by its organisation as well as the run: run ids are
    # each store's own, and repeat across organisations.
    node_run = next(
        m["agent_run_id"] for m in traced.calls if "workflow:Flow" in m["langfuse_tags"]
    )
    for run_id in (run["id"], fired["last_run_id"], invocation["agent_run_id"], node_run):
        sessions = {
            m.get("langfuse_session_id") for m in traced.calls if m.get("agent_run_id") == run_id
        }
        assert sessions == {f"agent-run-{org}-{run_id}"}
