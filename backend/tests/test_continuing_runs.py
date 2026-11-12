"""Continuing runs (phase 3.1): an agent keeps one run per chat. Each mention of it there is a
new turn of that run, on the agent's current version, briefed on what the chat said since its
previous turn; mentions made while a turn is under way, or while the run waits on an approval,
queue and run in message order."""

import asyncio
from datetime import timedelta

from conftest import capture_runs, drain_run, store_of
from sqlalchemy import func, update
from test_agent_archive import archive
from test_agents import agent_body
from test_chat_agent_invocations import replies_from, say
from test_chats import add, make_chat, messages, post
from test_delivery import fire
from test_schema_change_requests import (  # noqa: F401 (fixtures)
    CONTACTS,
    agent,
    queued,
    run_queued,
    store,
)
from test_triggers import make_agent, make_trigger, schedule
from test_workflow_runs import echo, start
from test_workflows import agent_node, make_workflow

from aios.config import get_settings
from aios.datastores.models import Invocation
from aios.run_service import sweep
from aios.testing.fake_model import (
    ToolCall,
    last_tool_results,
    model,
    replies,
    use_handoff,
    use_model,
)
from aios.tools import TOOLS


def mention(bot: dict) -> tuple[str, dict]:
    return ("agent", bot)


def token(bot: dict) -> str:
    return f"@[{bot['name']}](bot:{bot['id']})"


def run_page(api, bot: dict, run_id: int) -> dict:
    resp = api.get(f"/api/agents/{bot['id']}/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def cards(api, chat: dict) -> list[dict]:
    """The chat's invocations, oldest first."""
    return messages(api, chat)["invocations"]


def test_two_mentions_in_one_chat_are_two_turns_of_one_run(api):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)

    with use_model(replies("Hi.")):
        say(api, chat, f"{token(bot)} one", mention(bot))
        say(api, chat, f"{token(bot)} two", mention(bot))

    first, second = cards(api, chat)
    assert (first["status"], second["status"]) == ("succeeded", "succeeded")
    assert first["agent_run_id"] == second["agent_run_id"] is not None
    run = run_page(api, bot, first["agent_run_id"])
    assert [t["invocation_id"] for t in run["turns"]] == [first["id"], second["id"]]
    assert replies_from(api, chat) == [("Bot", "Hi."), ("Bot", "Hi.")]


def test_mentions_in_two_chats_are_two_separate_runs(api):
    bot = make_agent(api, "Bot")
    sales, launch = make_chat(api, "Sales"), make_chat(api, "Launch")
    add(api, sales, "agent", bot)
    add(api, launch, "agent", bot)

    with use_model(replies("Hi.")):
        say(api, sales, f"{token(bot)} one", mention(bot))
        say(api, launch, f"{token(bot)} two", mention(bot))
        say(api, sales, f"{token(bot)} three", mention(bot))

    in_sales, in_launch = cards(api, sales), cards(api, launch)
    assert in_sales[0]["agent_run_id"] == in_sales[1]["agent_run_id"]
    assert in_launch[0]["agent_run_id"] != in_sales[0]["agent_run_id"]
    assert len(run_page(api, bot, in_sales[0]["agent_run_id"])["turns"]) == 2
    assert len(run_page(api, bot, in_launch[0]["agent_run_id"])["turns"]) == 1


def test_a_later_turns_brief_covers_only_the_chat_since_the_agents_previous_turn(api):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    briefed: list[str] = []

    async def handoff(msgs, info):
        briefed.append(msgs[-1].content)
        yield ToolCall("Handoff", {"ack": "On it.", "brief": ""})

    with use_model(replies("First answer.")), use_handoff(model(handoff)):
        post(api, chat, "early chatter")
        say(api, chat, f"{token(bot)} first question", mention(bot))
        post(api, chat, "middle chatter")
        say(api, chat, f"{token(bot)} second question", mention(bot))

    first, second = briefed
    assert "early chatter" in first and "first question" in first
    assert "middle chatter" in second and "First answer." in second
    assert "second question" in second
    assert "early chatter" not in second and "first question" not in second


def test_each_turn_runs_on_the_agents_current_version_and_the_run_page_shows_which(api):
    bot = make_agent(api, "Bot")
    resp = api.put(f"/api/agents/{bot['id']}", json=agent_body(bot, base_prompt="Be terse."))
    bot = resp.json()
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    prompts: list[str] = []

    async def script(msgs, info):
        prompts.append(msgs[0].content)
        yield "Hi."

    with use_model(model(script)):
        say(api, chat, f"{token(bot)} one", mention(bot))
        # An editor saves a fix: the next turn in the existing conversation runs on it.
        resp = api.put(f"/api/agents/{bot['id']}", json=agent_body(bot, base_prompt="Be kind."))
        assert resp.json()["current_version"]["version"] == 3
        say(api, chat, f"{token(bot)} two", mention(bot))

    assert "Be terse." in prompts[0] and "Be kind." in prompts[1]
    [card, *_] = cards(api, chat)
    run = run_page(api, bot, card["agent_run_id"])
    assert [t["agent_version"] for t in run["turns"]] == [2, 3]
    assert run["agent_version"] == 3


def test_a_current_version_that_cant_run_fails_the_turn_and_the_run_keeps_its_version(
    api, monkeypatch
):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    with use_model(replies("Hi.")):
        say(api, chat, f"{token(bot)} one", mention(bot))
    resp = api.put(
        f"/api/agents/{bot['id']}",
        json=agent_body(bot, tools=[{"key": "web_search", "config": {}}]),
    )
    assert resp.status_code == 200, resp.text
    # Its tool leaves the library: the version saved can't run.
    monkeypatch.delitem(TOOLS, "web_search")
    calls = []

    async def script(msgs, info):
        calls.append(msgs)
        yield "never"

    with use_model(model(script)):
        say(api, chat, f"{token(bot)} two", mention(bot))

    assert calls == []
    first, second = cards(api, chat)
    assert second["status"] == "failed" and "web_search" in second["error"]
    assert second["agent_run_id"] == first["agent_run_id"]
    run = run_page(api, bot, first["agent_run_id"])
    assert [(t["agent_version"], t["status"]) for t in run["turns"]] == [
        (1, "succeeded"),
        (2, "failed"),
    ]
    assert "web_search" in run["turns"][1]["error"]
    assert run["agent_version"] == 1


def text_of(message) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


def test_a_mention_made_during_a_turn_waits_for_it_and_runs_next(api):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    mid_turn: list[dict] = []
    seen: list[list[str]] = []

    async def script(msgs, info):
        asked = [text_of(m) for m in msgs if m.type == "human"]
        seen.append(asked)
        if len(asked) == 1:
            # Someone mentions it again while it's still replying.
            await asyncio.to_thread(say, api, chat, f"{token(bot)} two", mention(bot))
            mid_turn.append(await asyncio.to_thread(messages, api, chat))
            yield "Answer one."
        else:
            yield "Answer two."

    with use_model(model(script)):
        say(api, chat, f"{token(bot)} one", mention(bot))

    # Mid-turn, the second waits for the first.
    first, second = mid_turn[0]["invocations"]
    assert (first["status"], second["status"]) == ("running", "queued")
    assert second["waiting_for"] == "turn" and first["waiting_for"] is None
    # Then it ran, after it, in the same run: its turn saw the first's reply.
    assert replies_from(api, chat) == [("Bot", "Answer one."), ("Bot", "Answer two.")]
    assert len(seen) == 2 and "one" in seen[1][0] and "two" in seen[1][1]
    first, second = cards(api, chat)
    assert second["status"] == "succeeded" and second["waiting_for"] is None
    assert first["agent_run_id"] == second["agent_run_id"]


def test_queued_mentions_run_in_message_order(api, run_service):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    run_service.auto = False
    for word in ("one", "two", "three"):
        say(api, chat, f"{token(bot)} {word}", mention(bot))
    assert [c["waiting_for"] for c in cards(api, chat)] == [None, "turn", "turn"]
    asked: list[str] = []

    async def script(msgs, info):
        asked.append(text_of([m for m in msgs if m.type == "human"][-1]))
        yield f"Reply {len(asked)}."

    with use_model(model(script)):
        run_service.drain()

    assert ["one" in asked[0], "two" in asked[1], "three" in asked[2]] == [True] * 3
    assert replies_from(api, chat) == [
        ("Bot", "Reply 1."),
        ("Bot", "Reply 2."),
        ("Bot", "Reply 3."),
    ]
    assert len({c["agent_run_id"] for c in cards(api, chat)}) == 1


class AsksThenAnswers:
    """The model: a turn given "change" asks for the CONTACTS change, and once told the outcome
    says "Changed."; any other turn says what it was asked. Records each turn's system prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def __call__(self, msgs, info):
        self.prompts.append(text_of(msgs[0]))
        if last_tool_results(msgs):
            yield "Changed."
            return
        asked = text_of([m for m in msgs if m.type == "human"][-1])
        if "change" in asked:
            yield ToolCall(
                "request_schema_change", {"ops": [CONTACTS], "reason": "To keep track of people"}
            )
        else:
            yield "Answered: " + ("second" if "second" in asked else "other")


def waiting_run(api, bot: dict, chat: dict, script) -> dict:
    """Mention the agent with a request for a change: its turn ends waiting on the approval.
    The card."""
    with use_model(model(script)):
        say(api, chat, f"{token(bot)} please change it", mention(bot))
    [card] = cards(api, chat)
    assert card["status"] == "running" and card["agent_run"]["waiting"] is True
    return card


def test_a_mention_while_the_run_waits_on_an_approval_queues_until_its_decided(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
):
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    script = AsksThenAnswers()
    card = waiting_run(api, agent, chat, script)
    original = script.prompts[0]
    # An editor saves a new version meanwhile.
    resp = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, base_prompt="Newer."))
    assert resp.status_code == 200, resp.text

    with use_model(model(script)):
        say(api, chat, f"{token(agent)} the second question", mention(agent))
    first, second = cards(api, chat)
    assert (second["status"], second["waiting_for"]) == ("queued", "approval")
    assert second["agent_run"] is None

    request_id = run_page(api, agent, card["agent_run_id"])["schema_change_requests"][0]["id"]
    assert api.post(f"/api/approvals/{request_id}/approve").status_code == 200
    with use_model(model(script)):
        run_queued(queued)

    first, second = cards(api, chat)
    assert (first["status"], second["status"]) == ("succeeded", "succeeded")
    assert replies_from(api, chat) == [("Builder", "Changed."), ("Builder", "Answered: second")]
    # The waiting turn finished on the version it started with; the next ran on the new one.
    assert script.prompts[1] == original and "Newer." in script.prompts[2]
    run = run_page(api, agent, card["agent_run_id"])
    versions = [(t["invocation_id"], t["agent_version"]) for t in run["turns"]]
    old = agent["current_version"]["version"]
    assert versions == [(first["id"], old), (first["id"], old), (second["id"], old + 1)]


def test_a_cancelled_approval_frees_the_queue(api, agent, queued):  # noqa: F811
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    script = AsksThenAnswers()
    card = waiting_run(api, agent, chat, script)
    with use_model(model(script)):
        say(api, chat, f"{token(agent)} the second question", mention(agent))
        # Deleting the waiting run cancels its approval.
        resp = api.delete(f"/api/agents/{agent['id']}/runs/{card['agent_run_id']}")
        assert resp.status_code == 204

    first, second = cards(api, chat)
    assert first["status"] == "failed" and first["run_deleted"] is True
    assert second["status"] == "succeeded"
    assert second["agent_run_id"] not in (None, card["agent_run_id"])
    assert replies_from(api, chat)[-1] == ("Builder", "Answered: second")


def test_a_mention_of_an_archived_agent_is_refused_on_its_card(api):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    with use_model(replies("Hi.")):
        say(api, chat, f"{token(bot)} one", mention(bot))
    archive(api, bot)

    with use_model(replies("never")):
        say(api, chat, f"{token(bot)} two", mention(bot))

    first, second = cards(api, chat)
    assert second["status"] == "refused"
    assert second["refusal_reason"] == "The agent Bot is archived."
    assert second["agent_run_id"] is None
    assert replies_from(api, chat) == [("Bot", "Hi.")]
    assert len(run_page(api, bot, first["agent_run_id"])["turns"]) == 1


def test_a_trigger_delivering_to_a_chat_doesnt_join_the_agents_conversation_there(api, db):
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Ops")
    t = make_trigger(api, schedule("agent", bot["id"], deliver_to=chat["id"], input="Report"))
    with use_model(replies("Hi.")):
        say(api, chat, f"{token(bot)} one", mention(bot))
        fire(db, t)
        say(api, chat, f"{token(bot)} two", mention(bot))

    runs = api.get(f"/api/agents/{bot['id']}/runs").json()
    assert len(runs) == 2
    mentioned = [c for c in cards(api, chat) if not c["delivered"]]
    [conversation] = [r for r in runs if r["id"] == mentioned[0]["agent_run_id"]]
    [scheduled] = [r for r in runs if r["id"] != conversation["id"]]
    assert mentioned[1]["agent_run_id"] == conversation["id"]
    assert len(run_page(api, bot, conversation["id"])["turns"]) == 2
    assert len(run_page(api, bot, scheduled["id"])["turns"]) == 1
    assert scheduled["trigger_id"] == t["id"]


def test_a_mention_waiting_its_turn_isnt_requeued_as_lost(api, agent, queued, in_store):  # noqa: F811
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    waiting_run(api, agent, chat, AsksThenAnswers())
    with use_model(replies("never")):
        say(api, chat, f"{token(agent)} the second question", mention(agent))
    long_ago = func.now() - timedelta(seconds=get_settings().queue_requeue_after_seconds + 1)
    in_store.execute(update(Invocation).values(created_at=long_ago))

    # It isn't lost: it waits for the approval.
    assert sweep.requeue() == 0
    assert cards(api, chat)[1]["status"] == "queued"


def test_a_workflow_nodes_run_keeps_its_pinned_version_and_isnt_a_conversation(
    api, user, monkeypatch
):
    bot = make_agent(api, "Bot")
    bot = api.put(f"/api/agents/{bot['id']}", json=agent_body(bot, base_prompt="old")).json()
    workflow = make_workflow(api, "Flow", [agent_node("a", bot)])
    resp = api.put(f"/api/agents/{bot['id']}", json=agent_body(bot, base_prompt="new"))
    newer = resp.json()["current_version"]["version"]
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    with use_model(echo()):
        say(api, chat, f"{token(bot)} hi", mention(bot))
    runs = capture_runs(monkeypatch, store_of(user))

    started = start(api, workflow, "go")
    with use_model(echo()):
        drain_run(runs.store, started["id"])

    out = api.get(f"/api/workflows/{workflow['id']}/runs/{started['id']}").json()
    [step] = out["steps"]
    assert step["output"] == "old(go)"
    node_run = run_page(api, bot, step["agent_run_id"])
    assert [t["agent_version"] for t in node_run["turns"]] == [newer - 1]
    [card] = cards(api, chat)
    assert card["agent_run_id"] != step["agent_run_id"]
    assert run_page(api, bot, card["agent_run_id"])["agent_version"] == newer


def test_turns_in_two_chats_run_at_the_same_time(api):
    bot = make_agent(api, "Bot")
    sales, launch = make_chat(api, "Sales"), make_chat(api, "Launch")
    add(api, sales, "agent", bot)
    add(api, launch, "agent", bot)
    answered_meanwhile: list[bool] = []

    async def script(msgs, info):
        asked = text_of([m for m in msgs if m.type == "human"][-1])
        if "sales" in asked:
            # Mentioned in the other chat while this turn is under way: that turn runs now.
            await asyncio.to_thread(say, api, launch, f"{token(bot)} launch", mention(bot))
            for _ in range(100):
                if await asyncio.to_thread(replies_from, api, launch):
                    break
                await asyncio.sleep(0.1)
            answered_meanwhile.append(bool(await asyncio.to_thread(replies_from, api, launch)))
        yield "Done."

    with use_model(model(script)):
        say(api, sales, f"{token(bot)} sales", mention(bot))

    assert answered_meanwhile == [True]
    [in_sales], [in_launch] = cards(api, sales), cards(api, launch)
    assert in_sales["agent_run_id"] != in_launch["agent_run_id"]


def test_a_turn_ending_on_an_approval_shows_its_card_waiting_on_the_stream(
    api,
    agent,  # noqa: F811
):
    """A turn that ends waiting on an approval finishes no invocation and posts no message: its
    card, open in the chat, still says it's waiting (the full-stack story's first step)."""
    from sse import open_stream

    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    with open_stream(api, f"/api/chats/{chat['id']}/stream") as stream:
        with use_model(model(AsksThenAnswers())):
            say(api, chat, f"{token(agent)} please change it", mention(agent))
        events = stream.until(
            lambda es: any(
                e.event == "invocation" and (e.data.get("agent_run") or {}).get("waiting")
                for e in es
            )
        )
    [card] = [e.data for e in events if e.event == "invocation"][-1:]
    assert card["status"] == "running" and card["agent_run"]["waiting"] is True
