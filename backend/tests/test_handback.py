"""Results coming back to a chat are summarised by the handback before they're posted, with the
full output kept behind the card's disclosure."""

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import drain_run, user_store
from langchain_core.callbacks import BaseCallbackHandler
from sqlalchemy import func, select, update
from test_chat_agent_invocations import replies_from, say
from test_chat_invocations import echo_agent, queued  # noqa: F401 (a fixture)
from test_chat_invocations import say as say_workflow
from test_chats import add, make_chat, messages
from test_triggers import make_agent
from test_workflow_runs import ProcessDied, echo, lapse_leases
from test_workflows import agent_node, make_workflow

from aios import handback, invocations, jev, llm, tracing
from aios.config import get_settings
from aios.datastores.models import ChatMessage, Invocation, UsageEntry

# The handback's model as the app gets it: tests run it on `fake_model.summarises` by default.
from aios.handback import model as real_handback_model
from aios.models import UsagePurpose
from aios.run_service import sweep
from aios.testing.fake_model import (
    ToolCall,
    Usage,
    model,
    replies,
    summarises,
    untrusted_blocks,
    use_handback,
    use_handoff,
    use_model,
)


def seeing(seen: list, summary: str):
    """A handback model answering `summary`, noting what it was sent."""

    async def script(msgs, info):
        seen.append(msgs)
        yield ToolCall("Handback", {"summary": summary})

    return model(script)


def test_a_mentioned_workflows_result_is_posted_as_its_summary(api, queued):  # noqa: F811
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    say_workflow(api, chat, f"@[Digest](workflow:{wf['id']}) sum it up", wf)
    seen = []

    with use_model(echo()), use_handback(seeing(seen, "Summed up: all is well.")):
        drain_run(queued.store, queued[0])

    assert replies_from(api, chat, "workflow") == [("Digest", "Summed up: all is well.")]
    [invocation] = messages(api, chat)["invocations"]
    given = jev.wrap("@Digest sum it up", "chat message")
    assert invocation["status"] == "succeeded"
    assert invocation["summary"] == "Summed up: all is well."
    assert invocation["full_output"] == f"first({given})"
    assert invocation["handback_error"] is None
    # It was sent the asking message, and the output, each in an untrusted block.
    [sent] = seen
    blocks = dict(untrusted_blocks(sent))
    assert blocks["chat message"] == "@Digest sum it up"
    assert blocks["output"] == f"first({given})"


def bot_in_chat(api) -> tuple[dict, dict]:
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    return agent, chat


def test_an_agents_short_reply_is_posted_as_written(api):
    agent, chat = bot_in_chat(api)
    seen = []

    with use_model(replies("Short and sweet.")), use_handback(seeing(seen, "Never posted.")):
        say(api, chat, "Go", ("agent", agent))

    assert seen == []
    assert replies_from(api, chat) == [("Bot", "Short and sweet.")]
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["full_output"], invocation["summary"]) == (None, None)


def test_an_agents_reply_over_the_threshold_is_summarised(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "handback_threshold", 50)
    agent, chat = bot_in_chat(api)
    long = "word " * 20

    with use_model(replies(long)), use_handback(summarises("In short: words.")):
        say(api, chat, "Go", ("agent", agent))

    assert replies_from(api, chat) == [("Bot", "In short: words.")]
    [invocation] = messages(api, chat)["invocations"]
    assert invocation["full_output"] == long
    assert invocation["summary"] == "In short: words."


def test_an_agents_reply_in_json_is_summarised_however_short(api):
    agent, chat = bot_in_chat(api)
    reply = '{"total": 3, "items": ["a", "b", "c"]}'

    with use_model(replies(reply)), use_handback(summarises("Three items: a, b and c.")):
        say(api, chat, "Go", ("agent", agent))

    assert replies_from(api, chat) == [("Bot", "Three items: a, b and c.")]
    [invocation] = messages(api, chat)["invocations"]
    assert json.loads(invocation["full_output"]) == {"total": 3, "items": ["a", "b", "c"]}


def test_the_card_says_its_handing_back_while_the_handback_runs(api):
    agent, chat = bot_in_chat(api)
    mid_call = []

    async def script(msgs, info):
        mid_call.append(await asyncio.to_thread(messages, api, chat))
        yield ToolCall("Handback", {"summary": "Done."})

    with use_model(replies("[1, 2]")), use_handback(model(script)):
        say(api, chat, "Go", ("agent", agent))

    [seen] = mid_call
    [invocation] = seen["invocations"]
    assert invocation["status"] == "handing_back"
    assert invocation["full_output"] == "[1, 2]" and invocation["finished_at"] is None
    # Nothing is posted until the handback is done.
    assert [
        m for m in seen["messages"] if m["kind"] == "text" and m["author"]["kind"] == "agent"
    ] == []


def failing(exc: Exception):
    async def script(msgs, info):
        raise exc
        yield

    return model(script)


@pytest.mark.parametrize(
    ("handback", "why"),
    [
        (failing(RuntimeError("model overloaded")), "model overloaded"),
        # A model that answers without the structured `{summary}`.
        (replies("Sure!"), "the model gave no summary"),
    ],
)
def test_a_failed_handback_posts_the_raw_output_cut_short_with_a_note(
    api, monkeypatch, handback, why
):
    monkeypatch.setattr(get_settings(), "handback_raw_limit", 100)
    agent, chat = bot_in_chat(api)
    reply = "[" + ", ".join(str(n) for n in range(100)) + "]"

    with use_model(replies(reply)), use_handback(handback):
        say(api, chat, "Go", ("agent", agent))

    [(_, posted)] = replies_from(api, chat)
    assert posted.startswith(reply[:100])
    assert reply not in posted
    assert (
        f"Couldn't summarise this result ({why}), so it's posted as it came, cut short." in posted
    )
    [invocation] = messages(api, chat)["invocations"]
    # The invocation still succeeds, with the full output kept and why summarising failed.
    assert (invocation["status"], invocation["error"]) == ("succeeded", None)
    assert (invocation["full_output"], invocation["summary"]) == (reply, None)
    assert invocation["handback_error"] == why


def test_a_handback_that_times_out_posts_the_raw_output(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "handback_timeout_seconds", 0.2)
    agent, chat = bot_in_chat(api)

    async def slow(msgs, info):
        await asyncio.sleep(5)
        yield ToolCall("Handback", {"summary": "Too late."})

    with use_model(replies("[1, 2, 3]")), use_handback(model(slow)):
        say(api, chat, "Go", ("agent", agent))

    [(_, posted)] = replies_from(api, chat)
    assert posted.startswith("[1, 2, 3]\n\n")
    assert "Couldn't summarise this result (it took too long)" in posted
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["handback_error"]) == ("succeeded", "it took too long")


def handback_dies(monkeypatch) -> None:
    """The handback's process dies mid-call: nothing gets to record what happened."""

    async def dies(request):
        raise ProcessDied

    monkeypatch.setattr(handback, "_ask", dies)


def test_a_handback_whose_lease_was_abandoned_is_tried_again(api, run_service, monkeypatch):
    agent, chat = bot_in_chat(api)
    with monkeypatch.context() as patched, use_model(replies("[1, 2]")):
        handback_dies(patched)
        say(api, chat, "Go", ("agent", agent))
    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "handing_back"
    # Its process died holding it: once its lease lapses, the run service tries it again.
    lapse_leases()
    with use_handback(summarises("Two numbers.")):
        run_service.drain()

    assert replies_from(api, chat) == [("Bot", "Two numbers.")]
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["summary"]) == ("succeeded", "Two numbers.")


def test_a_handback_whose_attempts_ran_out_posts_the_raw_output(api, run_service, monkeypatch):
    agent, chat = bot_in_chat(api)
    handback_dies(monkeypatch)
    with use_model(replies("[1, 2]")):
        say(api, chat, "Go", ("agent", agent))
        for _ in range(get_settings().queue_max_attempts):
            lapse_leases()
            run_service.drain()

    [(_, posted)] = replies_from(api, chat)
    assert posted.startswith("[1, 2]\n\n_Couldn't summarise this result (the service stopped")
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["full_output"]) == ("succeeded", "[1, 2]")
    assert invocation["handback_error"] == "the service stopped while summarising it"


def test_a_lost_handback_enqueue_is_repaired_by_the_requeue_sweep(
    api, in_store, run_service, monkeypatch
):
    agent, chat = bot_in_chat(api)
    with monkeypatch.context() as patched, use_model(replies("[1, 2]")):
        # The process stops between ending the turn and enqueueing its handback.
        patched.setattr(invocations, "enqueue_handbacks", lambda *args: None)
        say(api, chat, "Go", ("agent", agent))
    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "handing_back"
    in_store.execute(
        update(Invocation)
        .where(Invocation.id == invocation["id"])
        .values(
            created_at=func.now()
            - timedelta(seconds=get_settings().queue_requeue_after_seconds + 1)
        )
    )
    assert sweep.requeue() == 1
    assert sweep.requeue() == 0
    run_service.drain()
    assert replies_from(api, chat) == [("Bot", "[1, 2]")]
    assert messages(api, chat)["invocations"][0]["status"] == "succeeded"


class Spy(BaseCallbackHandler):
    """Notes the metadata of each chat model call it's told of."""

    def __init__(self):
        self.calls = []

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
        self.calls.append(metadata or {})


def test_each_handback_is_traced_and_recorded_in_the_usage_ledger(api, user, monkeypatch):
    spy = Spy()
    monkeypatch.setattr(tracing, "callbacks", lambda: [spy])
    agent, chat = bot_in_chat(api)

    async def script(msgs, info):
        yield Usage(30, 3)
        yield ToolCall("Handback", {"summary": "Two numbers."})

    with use_model(replies("[1, 2]")), use_handback(model(script)):
        say(api, chat, "Go", ("agent", agent))

    [invocation] = messages(api, chat)["invocations"]
    [traced] = [m for m in spy.calls if "handback" in m.get("langfuse_tags", [])]
    assert (traced["invocation_id"], traced["chat_id"]) == (invocation["id"], chat["id"])
    with user_store(user) as store:
        [row] = store.scalars(
            select(UsageEntry).where(UsageEntry.purpose == UsagePurpose.handback)
        ).all()
    assert (row.invocation_id, row.agent_id, row.person_id) == (
        invocation["id"],
        agent["id"],
        user.id,
    )
    assert (row.input_tokens, row.output_tokens, row.cause) == (30, 3, "mention")


def test_handbacks_are_derived_and_reach_later_turns_wrapped_as_untrusted(api, user):
    agent, chat = bot_in_chat(api)
    with use_model(replies("[1, 2]")), use_handback(summarises("Two numbers: 1 and 2.")):
        say(api, chat, "Go", ("agent", agent))
    with user_store(user) as store:
        verdicts = store.scalars(
            select(ChatMessage.verdict).where(
                ChatMessage.chat_id == chat["id"], ChatMessage.author_kind == "agent"
            )
        ).all()
    assert set(verdicts) == {jev.Verdict.derived}
    seen = []

    async def handoff(msgs, info):
        seen.append(msgs)
        yield ToolCall("Handoff", {"ack": "On it.", "brief": ""})

    with use_handoff(model(handoff)), use_model(replies("Ok.")):
        say(api, chat, "And again", ("agent", agent))

    [sent] = seen
    blocks = untrusted_blocks(sent)
    # The handback is inside the chat's untrusted block, never outside one.
    assert any("Two numbers: 1 and 2." in body for _, body in blocks)
    outside = "".join(m.content for m in sent)
    for _, body in blocks:
        outside = outside.replace(body, "")
    assert "Two numbers: 1 and 2." not in outside


def test_the_handback_runs_on_the_configured_model_else_the_handoffs(monkeypatch):
    asked = []
    monkeypatch.setattr(llm, "chat_model", lambda name=None: asked.append(name) or summarises())
    real_handback_model()
    monkeypatch.setattr(get_settings(), "handoff_model", "anthropic:claude-haiku-5")
    real_handback_model()
    monkeypatch.setattr(get_settings(), "handback_model", "anthropic:claude-opus-5")
    real_handback_model()
    # By default, the deployment's default model.
    assert asked == [None, "anthropic:claude-haiku-5", "anthropic:claude-opus-5"]
