"""Mentioning an agent in a chat runs a turn of it, followed by a card and posted back."""

import asyncio
from datetime import timedelta

from sqlalchemy import func, update
from test_agent_run_counters import searching_agent
from test_chat_invocations import echo_agent, queued  # noqa: F401 (a fixture)
from test_chats import add, make_chat, messages
from test_triggers import make_agent
from test_workflows import agent_node, make_workflow

from aios import checkpoints, firing, scheduler
from aios.config import get_settings
from aios.models import AgentRun, ChatInvocation
from aios.testing.fake_model import ToolCall, Usage, model, replies, use_model
from aios.tools import TOOLS


def say(api, chat: dict, body: str, *mentioned: tuple[str, dict]) -> dict:
    """Post a message mentioning the (kind, member)s; returns `{messages, invocations}`."""
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": body, "mentions": [{"kind": k, "id": m["id"]} for k, m in mentioned]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def counters(run: dict) -> tuple:
    return (run["status"], run["tool_call_count"], run["input_tokens"], run["output_tokens"])


def replies_from(api, chat: dict, kind: str = "agent") -> list[tuple[str, str]]:
    """The chat's messages from its agents (or workflows) that aren't cards: who, and what."""
    return [
        (m["author"]["name"], m["body"])
        for m in messages(api, chat)["messages"]
        if m["author"] and m["author"]["kind"] == kind and m["kind"] == "text"
    ]


def test_mentioning_an_agent_runs_a_turn_followed_by_its_card_and_posts_the_reply(
    api, user, monkeypatch
):
    agent, runs_url = searching_agent(api, monkeypatch)
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    seen_mid_turn = []

    async def script(msgs, info):
        if msgs[-1].type == "human":
            yield Usage(100, 10)
            yield ToolCall("search_documents", {"query": "apples"})
            yield ToolCall("search_documents", {"query": "pears"})
        else:
            # The chat's poll, while the turn is still going.
            seen_mid_turn.append(await asyncio.to_thread(messages, api, chat))
            yield "Found both."
            yield Usage(250, 5)

    with use_model(model(script)):
        posted = say(api, chat, f"@[Bot](agent:{agent['id']}) find fruit", ("agent", agent))

    # The acknowledgement and card come with the message, before the turn starts.
    message, card = posted["messages"]
    assert (card["kind"], card["author"]["kind"], card["author"]["name"]) == (
        "run_card",
        "agent",
        "Bot",
    )
    [invocation] = posted["invocations"]
    assert card["invocation_id"] == invocation["id"]
    assert (invocation["target_kind"], invocation["target_id"]) == ("agent", agent["id"])
    assert (invocation["status"], invocation["requested_by"]) == ("queued", user.id)
    assert invocation["agent_run_id"] is None and invocation["agent_run"] is None

    # Mid-turn, the poll carries the run's start and its counters so far.
    [live] = seen_mid_turn[0]["invocations"]
    assert live["status"] == "running" and live["agent_run_id"]
    run = live["agent_run"]
    assert counters(run) == ("running", 2, 100, 10)
    assert run["started_at"] and not run["finished_at"]

    # When the turn ends, its reply is posted back from the agent.
    done = messages(api, chat, after=card["id"])
    [result] = done["messages"]
    assert (result["author"]["kind"], result["author"]["name"], result["body"]) == (
        "agent",
        "Bot",
        "Found both.",
    )
    assert result["invocation_id"] == invocation["id"]
    [finished] = done["invocations"]
    assert (finished["status"], finished["error"]) == ("succeeded", None)
    assert counters(finished["agent_run"]) == ("succeeded", 2, 350, 15)

    # The run is the agent's, run with the message (a mention as `@name`), marked as from the chat.
    run_id = live["agent_run_id"]
    history = checkpoints.history(run_id)
    assert history[0].text == "@Bot find fruit"
    [listed] = api.get(runs_url).json()
    assert (listed["id"], listed["chat_id"], listed["chat_name"]) == (run_id, chat["id"], "Design")
    assert messages(api, chat, after=result["id"]) == {"messages": [], "invocations": []}


def test_a_failed_turn_posts_its_error_and_fails_the_card(api):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)

    async def script(msgs, info):
        yield "Starting"
        raise RuntimeError("model overloaded")

    with use_model(model(script)):
        say(api, chat, "Go", ("agent", agent))
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == ("failed", "model overloaded")
    assert invocation["agent_run"]["status"] == "failed"
    assert replies_from(api, chat) == [("Bot", "The run failed: model overloaded")]


def age(db, invocation_id: int) -> None:
    """Make the invocation's and its run's heartbeats older than a live process lets them get."""
    stale = func.now() - firing.STALE_AFTER - timedelta(seconds=1)
    db.execute(
        update(ChatInvocation).where(ChatInvocation.id == invocation_id).values(heartbeat_at=stale)
    )
    db.execute(
        update(AgentRun)
        .where(AgentRun.id == ChatInvocation.agent_run_id, ChatInvocation.id == invocation_id)
        .values(heartbeat_at=stale)
    )
    db.commit()


def test_a_turn_whose_heartbeat_stops_is_failed_when_the_chat_is_read(api, db):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    seen = []

    async def script(msgs, info):
        # Its process looks dead to a reader: nothing has beaten it for too long.
        [invocation] = (await asyncio.to_thread(messages, api, chat))["invocations"]
        await asyncio.to_thread(age, db, invocation["id"])
        seen.append(await asyncio.to_thread(messages, api, chat))
        yield "Too late."

    with use_model(model(script)):
        say(api, chat, "Go", ("agent", agent))

    [stalled] = seen[0]["invocations"]
    assert (stalled["status"], stalled["error"]) == ("failed", firing.STOPPED)
    assert stalled["agent_run"]["status"] == "failed"
    # The turn that carried on regardless posts nothing more: the card had already failed.
    assert replies_from(api, chat) == [("Bot", f"The run failed: {firing.STOPPED}")]
    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "failed"


def test_a_turn_waits_for_a_free_slot_under_the_cap(api, monkeypatch):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    # Every slot is taken by other fires.
    monkeypatch.setattr(get_settings(), "scheduler_max_concurrent_runs", 1)
    monkeypatch.setattr(firing, "free_slots", lambda: 0)

    with use_model(replies("Hello.")):
        say(api, chat, "Go", ("agent", agent))
    [waiting] = messages(api, chat)["invocations"]
    assert (waiting["status"], waiting["agent_run_id"]) == ("queued", None)

    # A slot frees up: the scheduler's next tick, on any replica, runs it.
    monkeypatch.undo()

    async def tick():
        await scheduler.tick()
        await asyncio.gather(*firing._in_flight)

    with use_model(replies("Hello.")):
        asyncio.run(tick())
    assert replies_from(api, chat) == [("Bot", "Hello.")]
    assert messages(api, chat)["invocations"][0]["status"] == "succeeded"


def test_agents_and_workflows_mentioned_together_each_start_and_replies_trigger_nothing(
    api,
    queued,  # noqa: F811
):
    one = make_agent(api, "One")
    two = make_agent(api, "Two")
    wf = make_workflow(api, "Digest", [agent_node("a", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    for kind, member in (("agent", one), ("agent", two), ("workflow", wf)):
        add(api, chat, kind, member)

    # Each agent's reply mentions the other: that starts nothing.
    async def script(msgs, info):
        yield f"Over to you, @[Two](agent:{two['id']}) and @[One](agent:{one['id']})"

    with use_model(model(script)):
        posted = say(api, chat, "All of you", ("agent", one), ("agent", two), ("workflow", wf))
    assert [m["author"]["name"] for m in posted["messages"][1:]] == ["One", "Two", "Digest"]
    assert len(queued) == 1
    reply = f"Over to you, @[Two](agent:{two['id']}) and @[One](agent:{one['id']})"
    assert sorted(replies_from(api, chat)) == [("One", reply), ("Two", reply)]
    assert len(messages(api, chat)["invocations"]) == 3


def test_a_turn_that_cant_start_fails_the_card_with_why(api, monkeypatch):
    agent, _ = searching_agent(api, monkeypatch)
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    monkeypatch.delitem(TOOLS, "hybrid_search")
    say(api, chat, "Go", ("agent", agent))
    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "failed" and "hybrid_search" in invocation["error"]
    assert invocation["agent_run_id"] is None and invocation["run_deleted"] is False
    [(name, body)] = replies_from(api, chat)
    assert name == "Bot" and body.startswith("Couldn't start the run: ")


def test_a_card_whose_run_was_deleted_says_so(api):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    with use_model(replies("Hi.")):
        say(api, chat, "Go", ("agent", agent))
    [invocation] = messages(api, chat)["invocations"]
    resp = api.delete(f"/api/agents/{agent['id']}/runs/{invocation['agent_run_id']}")
    assert resp.status_code == 204
    [after] = messages(api, chat)["invocations"]
    assert (after["status"], after["run_deleted"], after["agent_run"]) == ("succeeded", True, None)
