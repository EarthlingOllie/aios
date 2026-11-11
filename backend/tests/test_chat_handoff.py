"""Before a mention's run starts, the handoff briefs it on the chat and says what was understood."""

import asyncio

from conftest import store_of
from langchain_core.callbacks import BaseCallbackHandler
from test_chat_agent_invocations import replies_from, say
from test_chat_invocations import echo_agent, queued  # noqa: F401 (a fixture)
from test_chat_invocations import say as say_workflow
from test_chats import add, make_chat, messages, post
from test_triggers import make_agent
from test_workflows import agent_node, make_workflow

from aios import checkpoints, jev, llm, tracing
from aios.config import get_settings

# The handoff's model as the app gets it: tests run it on `fake_model.briefs` by default.
from aios.handoff import model as real_handoff_model
from aios.testing.fake_model import ToolCall, briefs, model, replies, use_handoff, use_model


def handoff_script(seen: list, api, chat: dict, ack: str, brief: str):
    """A handoff model answering `{ack, brief}`, noting what it was sent and the chat mid-call."""

    async def script(msgs, info):
        seen.append((msgs, await asyncio.to_thread(messages, api, chat)))
        yield ToolCall("Handoff", {"ack": ack, "brief": brief})

    return model(script)


def card_of(api, chat: dict) -> dict:
    [card] = [m for m in messages(api, chat)["messages"] if m["kind"] == "run_card"]
    return card


def test_the_handoff_briefs_an_agent_and_its_ack_replaces_the_placeholder(api, user):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    post(api, chat, "The Q3 figures are in the shared sheet.")
    seen = []
    ack = "I'll check the Q3 figures against the plan."
    brief = "The team is reviewing Q3; the figures are in the shared sheet."

    with (
        use_handoff(handoff_script(seen, api, chat, ack, brief)),
        use_model(replies("They match.")),
    ):
        posted = say(api, chat, f"@[Bot](agent:{agent['id']}) check them", ("agent", agent))

    # The card comes with the placeholder, before the handoff has run.
    placeholder = posted["messages"][1]["body"]
    assert placeholder != ack
    # While the handoff runs, the invocation is briefing, and the handoff was sent the chat.
    [(sent, mid_call)] = seen
    [briefing] = mid_call["invocations"]
    assert (briefing["status"], briefing["agent_run_id"]) == ("briefing", None)
    transcript = "\n".join(m.text for m in sent)
    assert "The Q3 figures are in the shared sheet." in transcript
    assert "@Bot check them" in transcript

    # The ack replaced the placeholder; the brief is stored on the invocation.
    assert card_of(api, chat)["body"] == ack
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["brief"]) == ("succeeded", brief)
    assert invocation["acknowledgement"] == ack
    # The run was told the brief (wrapped as untrusted: it's derived from the chat), a blank
    # line, then the message.
    history = checkpoints.history(store_of(user), invocation["agent_run_id"])
    assert history[0].text == f"{jev.wrap(brief, 'brief')}\n\n@Bot check them"
    # Named after what was asked, not the brief.
    [run] = api.get(f"/api/agents/{agent['id']}/runs").json()
    assert run["title"] == "@Bot check them"


def test_a_workflows_run_is_told_the_brief_then_the_message(api, queued):  # noqa: F811
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    post(api, chat, "We ship on Friday.")

    with use_handoff(briefs("I'll sum up the week.", "The release is on Friday.")):
        say_workflow(api, chat, f"@[Digest](workflow:{wf['id']}) sum it up", wf)

    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["brief"]) == ("running", "The release is on Friday.")
    assert invocation["acknowledgement"] == "I'll sum up the week."
    assert card_of(api, chat)["body"] == "I'll sum up the week."
    run = invocation["workflow_run"]
    assert run["input"] == f"{jev.wrap('The release is on Friday.', 'brief')}\n\n@Digest sum it up"


def failing_handoff():
    async def script(msgs, info):
        raise RuntimeError("model overloaded")
        yield

    return model(script)


def test_a_failed_handoff_fails_an_agents_invocation_with_its_error(api):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)

    with use_handoff(failing_handoff()), use_model(replies("Never sent.")):
        say(api, chat, "Go", ("agent", agent))

    [invocation] = messages(api, chat)["invocations"]
    error = "The handoff failed: model overloaded"
    assert (invocation["status"], invocation["error"]) == ("failed", error)
    assert invocation["agent_run_id"] is None and invocation["run_deleted"] is False
    assert replies_from(api, chat) == [("Bot", f"Couldn't start the run: {error}")]


def test_a_failed_handoff_fails_a_workflows_invocation_with_its_error(api, queued):  # noqa: F811
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)

    # A model that answers without the structured `{ack, brief}`.
    with use_handoff(replies("Sure!")):
        say_workflow(api, chat, "Go", wf)

    assert queued == []
    [invocation] = messages(api, chat)["invocations"]
    error = "The handoff failed: the model gave no brief."
    assert (invocation["status"], invocation["error"]) == ("failed", error)
    assert replies_from(api, chat, "workflow") == [("Digest", f"Couldn't start the run: {error}")]


def sent_chat(seen: list) -> str:
    """The chat the handoff was sent: its last message's text."""
    [(sent, _)] = seen
    return sent[-1].text


def test_the_handoff_sees_the_last_messages_of_the_chat(api, monkeypatch):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    for n in range(1, 6):
        post(api, chat, f"note-{n}")
    monkeypatch.setattr(get_settings(), "handoff_window_messages", 3)
    seen = []

    with use_handoff(handoff_script(seen, api, chat, "On it.", "")), use_model(replies("Ok.")):
        say(api, chat, "Go", ("agent", agent))

    # Three messages: two before the mention, then the mention itself.
    sent = sent_chat(seen)
    assert [n for n in range(1, 6) if f"note-{n}" in sent] == [4, 5]
    assert sent.index("note-4") < sent.index("note-5") < sent.index("[Test User (user)]: Go")


def test_the_handoff_sees_about_as_many_tokens_of_the_chat_as_the_window_allows(api, monkeypatch):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    for n in range(1, 4):
        post(api, chat, f"note-{n} " + "x" * 400)
    # About 250 tokens: the mention and two of the notes, of 100 tokens or so each.
    monkeypatch.setattr(get_settings(), "handoff_window_tokens", 250)
    seen = []

    with use_handoff(handoff_script(seen, api, chat, "On it.", "")), use_model(replies("Ok.")):
        say(api, chat, "Go", ("agent", agent))

    sent = sent_chat(seen)
    assert [n for n in range(1, 4) if f"note-{n}" in sent] == [2, 3]


def test_the_handoff_runs_on_the_configured_model(monkeypatch):
    asked = []
    monkeypatch.setattr(llm, "chat_model", lambda name=None: asked.append(name) or briefs())
    assert real_handoff_model() is not None
    monkeypatch.setattr(get_settings(), "handoff_model", "anthropic:claude-haiku-5")
    real_handoff_model()
    # By default, the deployment's default model.
    assert asked == [None, "anthropic:claude-haiku-5"]


class Spy(BaseCallbackHandler):
    """Notes the metadata of each chat model call it's told of."""

    def __init__(self):
        self.calls = []

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
        self.calls.append(metadata or {})


def test_the_handoff_is_traced_tagged_handoff_with_its_invocation(api, monkeypatch):
    spy = Spy()
    monkeypatch.setattr(tracing, "callbacks", lambda: [spy])
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)

    with use_model(replies("Ok.")):
        say(api, chat, "Go", ("agent", agent))

    [invocation] = messages(api, chat)["invocations"]
    handoffs = [m for m in spy.calls if "handoff" in m.get("langfuse_tags", [])]
    [traced] = handoffs
    assert traced["invocation_id"] == invocation["id"]
    assert traced["chat_id"] == chat["id"]
