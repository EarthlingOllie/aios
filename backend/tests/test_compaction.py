"""Compaction (phase 3.1): once the model's input for a turn passes the threshold, the oldest
whole turns are folded into a summary (with any previous one) by the handoff model, in what the
model sees; the latest turns stay verbatim. The run's stored history, its run page and search
keep every turn. Summaries are derived text, always wrapped as untrusted."""

import pytest
from conftest import signed_in_store
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_agents import agent_body
from test_chats import add, make_chat
from test_continuing_runs import cards, mention, run_page, say, token
from test_data_tools import data_agent, store  # noqa: F401 (fixtures)
from test_search import embedded, found  # noqa: F401 (a fixture)
from test_triggers import make_agent
from test_usage import reported

from aios import handoff, search_index, tracing
from aios.config import Settings, get_settings
from aios.testing.fake_model import ToolCall, Usage, model, use_compaction, use_model

SUMMARY_SOURCE = "derived:compaction"


def wrapped_summary(text: str) -> str:
    return f'<untrusted source="{SUMMARY_SOURCE}">\n{text}\n</untrusted>'


@pytest.fixture
def compacting(monkeypatch):
    """Compaction on any input at all, keeping the latest two turns verbatim."""
    monkeypatch.setattr(get_settings(), "compaction_threshold_tokens", 1)
    monkeypatch.setattr(get_settings(), "compaction_turns_kept", 2)


def text_of(message) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


def conversation(api, words: list[str], agent_script=None, summariser_script=None, tools=None):
    """Mention a new agent (with `tools`) once per word in a new chat; what its model was sent
    at each call, what the summariser was sent, the agent and the run's id."""
    bot = make_agent(api, "Bot")
    if tools:
        resp = api.put(f"/api/agents/{bot['id']}", json=agent_body(bot, tools=tools))
        assert resp.status_code == 200, resp.text
        bot = resp.json()
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    seen: list[list] = []
    folded: list[list] = []

    async def agent(msgs, info):
        seen.append(list(msgs))
        if agent_script is not None:
            async for item in agent_script(msgs, info):
                yield item
        else:
            yield "Noted."

    async def summariser(msgs, info):
        folded.append(list(msgs))
        if summariser_script is not None:
            async for item in summariser_script(msgs, info):
                yield item
        else:
            yield f"Summary {len(folded)}"

    with use_model(model(agent)), use_compaction(model(summariser)):
        for word in words:
            say(api, chat, f"{token(bot)} {word}", mention(bot))
    run_id = cards(api, chat)[0]["agent_run_id"]
    return seen, folded, bot, run_id


def test_crossing_the_threshold_folds_the_oldest_turns_and_keeps_the_latest(api, compacting):
    seen, folded, bot, run_id = conversation(api, ["alpha", "bravo", "charlie", "delta"])

    # Three turns in, the first is folded; four in, the second is, with the first's summary.
    assert len(folded) == 2
    first, second = (text_of(m[-1]) for m in folded)
    assert "alpha" in first and "bravo" not in first
    assert "Summary 1" in second and "bravo" in second and "alpha" not in second

    last = seen[-1]
    assert isinstance(last[0], SystemMessage)
    assert isinstance(last[1], HumanMessage)
    assert last[1].content == wrapped_summary("Summary 2")
    rest = " ".join(text_of(m) for m in last[2:])
    assert "charlie" in rest and "delta" in rest
    assert "alpha" not in rest and "bravo" not in rest
    # The two latest turns, verbatim: the question, the reply, the question.
    assert [type(m) for m in last[2:]] == [HumanMessage, AIMessage, HumanMessage]

    run = run_page(api, bot, run_id)
    turns = [t["id"] for t in run["turns"]]
    assert [s["last_turn_id"] for s in run["summaries"]] == [turns[0], turns[1]]


def test_the_settings_default_to_the_specs():
    settings = Settings()
    assert settings.compaction_threshold_tokens == 120_000
    assert settings.compaction_turns_kept == 6
    assert settings.compaction_summary_tokens == 4_000


def test_below_the_threshold_the_model_sees_every_turn(api):
    seen, folded, _, _ = conversation(api, ["alpha", "bravo", "charlie", "delta"])

    assert folded == []
    assert "alpha" in " ".join(text_of(m) for m in seen[-1])


def paired(messages) -> bool:
    """Whether every tool result follows its call, and every call has its result."""
    calls: set[str] = set()
    results: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage):
            calls |= {c["id"] for c in m.tool_calls}
        elif isinstance(m, ToolMessage):
            if m.tool_call_id not in calls:
                return False
            results.add(m.tool_call_id)
    return calls == results


def test_a_summary_never_parts_a_tool_call_from_its_result(api, compacting, monkeypatch):
    monkeypatch.setattr(get_settings(), "compaction_turns_kept", 1)

    async def agent(msgs, info):
        # Each turn calls a tool twice, then answers.
        rounds = sum(isinstance(m, ToolMessage) for m in msgs[msgs.index(last_human(msgs)) :])
        if rounds < 2:
            yield ToolCall("describe_data", {})
        else:
            yield "Done."

    seen, folded, _, _ = conversation(
        api,
        ["alpha", "bravo", "charlie"],
        agent_script=agent,
        tools=[{"key": "describe_data", "config": {}}],
    )

    assert folded, "nothing was compacted"
    for view in seen:
        assert paired(view), view
        # After the system prompt: a summary, or the run's first message; a person's either way.
        assert isinstance(view[1], HumanMessage)
    # Folded mid-turn, the turn underway is kept whole: its question, calls and results.
    last = seen[-1]
    assert last[1].content.startswith(f'<untrusted source="{SUMMARY_SOURCE}">')
    assert "charlie" in text_of(last[2])
    assert [type(m) for m in last[2:]] == [HumanMessage, AIMessage, ToolMessage] * 1 + [
        AIMessage,
        ToolMessage,
    ]


def last_human(msgs):
    return next(m for m in reversed(msgs) if isinstance(m, HumanMessage))


class Recorder(BaseCallbackHandler):
    """The chat model calls a run's trace would record: their tags, metadata and parent."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw):
        self.calls.append(
            {
                "tags": kw.get("tags") or [],
                "metadata": kw.get("metadata") or {},
                "parent": parent_run_id,
            }
        )


def test_each_compaction_is_charged_as_usage_and_traced_as_one(
    api,
    user,
    compacting,
    monkeypatch,
    data_agent,  # noqa: F811
):
    recorder = Recorder()
    monkeypatch.setattr(tracing, "callbacks", lambda: [recorder])

    async def summariser(msgs, info):
        yield "The story so far."
        yield Usage(300, 25)

    _, folded, bot, run_id = conversation(
        api, ["alpha", "bravo", "charlie"], summariser_script=summariser
    )

    assert len(folded) == 1
    rows = [r for r in reported(api, data_agent) if r["purpose"] == "compaction"]
    [row] = rows
    assert row["kind"] == "model_call"
    assert row["model"] == handoff.model_name()
    assert (row["agent_id"], row["agent_run_id"], row["person_id"]) == (bot["id"], run_id, user.id)
    assert (row["input_tokens"], row["output_tokens"]) == (300, 25)
    turns = run_page(api, bot, run_id)["turns"]
    assert row["turn_id"] == turns[-1]["id"]

    traced = [c for c in recorder.calls if "compaction" in c["tags"]]
    assert len(traced) == 1
    assert "compaction" in traced[0]["metadata"]["langfuse_tags"]
    assert traced[0]["metadata"]["agent_run_id"] == run_id
    # Part of the turn's own trace.
    assert traced[0]["parent"] is not None


def test_the_history_is_unchanged_and_every_turn_still_found(api, compacting, embedded):  # noqa: F811
    _, folded, bot, run_id = conversation(api, ["alpha", "bravo", "charlie", "delta"])
    assert len(folded) == 2

    run = run_page(api, bot, run_id)
    assert len(run["messages"]) == 8
    said = " ".join(str(m["content"]) for m in run["messages"])
    for word in ("alpha", "bravo", "charlie", "delta"):
        assert word in said
    assert "Summary" not in said

    search_index.embed_pending(signed_in_store())
    hits = found(api, "alpha", "run_message")
    assert any(h["agent_run_id"] == run_id and "alpha" in h["text"] for h in hits)


# --- Retention ---------------------------------------------------------------------------------


def test_retention_prunes_a_conversation_turn_by_turn_and_a_summary_once_its_turns_are_gone(
    api,
    compacting,
    embedded,  # noqa: F811
    monkeypatch,
):
    from test_retention import age, audit_entries, prune, set_retention

    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)
    seen: list[list] = []

    async def agent(msgs, info):
        seen.append(list(msgs))
        yield "Noted."

    async def summariser(msgs, info):
        yield f"Summary of {', '.join(w for w in WORDS if w in text_of(msgs[-1]))}"

    with use_model(model(agent)), use_compaction(model(summariser)):
        say(api, chat, f"{token(bot)} alpha", mention(bot))
        age(signed_in_store(), 10)
        for word in ("bravo", "charlie", "delta"):
            say(api, chat, f"{token(bot)} {word}", mention(bot))
    run_id = cards(api, chat)[0]["agent_run_id"]
    before = run_page(api, bot, run_id)
    t1, t2, t3, t4 = (t["id"] for t in before["turns"])
    # Folded at the third turn (the first), and at the fourth (the second, with that summary).
    assert [s["last_turn_id"] for s in before["summaries"]] == [t1, t2]

    assert set_retention(api, run_days=5, chat_days=None).status_code == 200
    prune()

    run = run_page(api, bot, run_id)
    # The old turn went, with its messages and search entries; the conversation stays.
    assert [t["id"] for t in run["turns"]] == [t2, t3, t4]
    said = " ".join(str(m["content"]) for m in run["messages"])
    assert "alpha" not in said and "bravo" in said and "delta" in said
    assert run["message_count"] == len(run["messages"]) == 6
    search_index.embed_pending(signed_in_store())
    assert not [h for h in found(api, "alpha", "run_message") if "alpha" in h["text"]]
    assert [h for h in found(api, "bravo", "run_message") if "bravo" in h["text"]]
    # The first summary covered only the turn that went; the second covers one still kept.
    assert [s["last_turn_id"] for s in run["summaries"]] == [t2]
    [entry] = audit_entries(api, "turns.pruned")
    assert entry["details"]["count"] == 1

    # The conversation carries on from the summary that's left (folding no more for now).
    monkeypatch.setattr(get_settings(), "compaction_threshold_tokens", 120_000)
    with use_model(model(agent)), use_compaction(model(summariser)):
        say(api, chat, f"{token(bot)} echo", mention(bot))
    last = seen[-1]
    assert last[1].content == wrapped_summary("Summary of alpha, bravo")
    assert paired(last)

    # Once every turn it covers is gone, so is the summary.
    age(signed_in_store(), 10)
    with use_model(model(agent)), use_compaction(model(summariser)):
        say(api, chat, f"{token(bot)} foxtrot", mention(bot))
    prune()
    run = run_page(api, bot, run_id)
    assert len(run["turns"]) == 1
    assert run["summaries"] == []
    said = " ".join(str(m["content"]) for m in run["messages"])
    assert "foxtrot" in said and "echo" not in said


WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
