"""Scripted models a running app can select (`aios.testing.scenarios`), behind a setting that's
off by default: the full-stack test's stand-in for the providers. A chat turn on the core loop's
scenario, end to end through the API: the mention, the request for a `notes` table, the card
waiting, the approval, the resume, the row written and the reply posted."""

import asyncio
import ipaddress
import socket
from unittest import mock

import pytest
import test_schema_change_requests
from conftest import drain_run, store_of
from pydantic import SecretStr
from test_chat_agent_invocations import replies_from, say
from test_chat_invocations import queued as queued_runs  # noqa: F401 (a fixture)
from test_chat_invocations import say as say_workflow
from test_chats import add, make_chat, messages
from test_data_tools import as_owner, provisioned
from test_schema_change_requests import run_queued
from test_workflows import agent_node, make_workflow

from aios import dispatcher, embeddings, handback, handoff, llm, streams, tracing
from aios.config import get_settings
from aios.main import create_app
from aios.models import DataStore
from aios.run_service import executor, sweep
from aios.testing import scenarios

# The tasks the code queued (applying a change, resuming a run), to run as the workers would.
queued = test_schema_change_requests.queued

# The handoff's and handback's own models, as the app has them: every test's are scripted by
# default (conftest).
APP_HANDOFF_MODEL = handoff.model
APP_HANDBACK_MODEL = handback.model

TOOLS = [
    {"key": "request_schema_change", "config": {}},
    {"key": "write_data", "config": {}},
]


def refuse(*args, **kwargs):
    raise AssertionError("a provider was called")


@pytest.fixture
def no_providers(monkeypatch):
    """Nothing reaches a provider: their clients can't be made, and no connection can leave this
    machine (the database, Redis and storage are local)."""
    monkeypatch.setattr(llm, "init_chat_model", refuse)
    monkeypatch.setattr(embeddings, "VoyageAIEmbeddings", refuse)
    monkeypatch.setattr(tracing, "Langfuse", refuse)
    connect = socket.socket.connect

    def local_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise AssertionError(f"a connection left the machine: {address}")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    embeddings.get_embedder.cache_clear()
    tracing._client.cache_clear()
    yield
    embeddings.get_embedder.cache_clear()
    tracing._client.cache_clear()


@pytest.fixture
def scripted(monkeypatch, no_providers):
    """Scripted models enabled, the deployment's model the core loop's scenario, and the
    handoff and handback on it too (the deployment's), with Langfuse configured as if for
    real."""
    settings = get_settings()
    monkeypatch.setattr(settings, "scripted_models_enabled", True)
    monkeypatch.setattr(settings, "llm_model", "scripted:core-loop")
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", SecretStr("sk-lf-test"))
    with (
        mock.patch.object(handoff, "model", APP_HANDOFF_MODEL),
        mock.patch.object(handback, "model", APP_HANDBACK_MODEL),
    ):
        yield


@pytest.fixture
def store(db, user) -> DataStore:
    return provisioned(db, store_of(user))


@pytest.fixture
def notes_agent(api, store) -> dict:
    """An agent that may ask for tables and write to them, with a real store."""
    agent = api.post("/api/agents", json={"name": "Notes", "tools": TOOLS})
    assert agent.status_code == 201, agent.text
    return agent.json()


def notes(store: DataStore) -> list[str]:
    with as_owner(store) as conn:
        return [r[0] for r in conn.execute("SELECT body FROM data.notes")]


def test_a_chat_turn_runs_the_core_loop_on_its_scenario_with_no_provider_called(
    api, store, notes_agent, scripted, queued
):
    chat = make_chat(api, "Notes")
    add(api, chat, "agent", notes_agent)

    posted = say(api, chat, f"@[Notes](agent:{notes_agent['id']}) buy milk", ("agent", notes_agent))

    # The handoff ran on the scenario too, and the turn ended waiting for the table.
    _, card = posted["messages"]
    [invocation] = messages(api, chat)["invocations"]
    assert invocation["acknowledgement"] == scenarios.ACK
    assert invocation["status"] == "running"
    assert invocation["agent_run"]["waiting"] is True
    assert replies_from(api, chat) == []
    [request] = api.get("/api/approvals").json()
    assert (request["status"], request["summary"]) == ("pending", "Create table data.notes")

    # Approved (as on the Data page), it's applied and the run resumes, writes and replies.
    assert api.post(f"/api/approvals/{request['id']}/approve").status_code == 200
    run_queued(queued)

    assert notes(store) == ["@Notes buy milk"]
    assert replies_from(api, chat) == [("Notes", scenarios.SAVED)]
    [invocation] = messages(api, chat, after=card["id"])["invocations"]
    assert invocation["status"] == "succeeded"
    assert invocation["agent_run"]["waiting"] is False


def test_a_table_written_trigger_fires_once_and_its_echo_is_refused(
    api, store, notes_agent, scripted, queued
):
    """The full-stack test's table written story, here: once the core loop has its notes table,
    shared with an agent that appends to it, a trigger on the table runs that agent once when
    the core loop notes something again; the agent's own note is its echo, which its trigger
    refuses."""
    chat = make_chat(api, "Notes")
    add(api, chat, "agent", notes_agent)
    mention = f"@[Notes](agent:{notes_agent['id']})"
    say(api, chat, f"{mention} buy milk", ("agent", notes_agent))
    [request] = api.get("/api/approvals").json()
    assert api.post(f"/api/approvals/{request['id']}/approve").status_code == 200
    run_queued(queued)
    assert notes(store) == ["@Notes buy milk"]
    # Its events dispatched (in the stack, within moments), before there's a trigger.
    dispatcher.drain_once(store.id)

    appender = api.post(
        "/api/agents", json={"name": "Appender", "base_prompt": scenarios.APPEND, "tools": TOOLS}
    ).json()
    # The notes table is the core loop's agent's: shared, so the appending agent may write it.
    shared = api.post("/api/data/tables/notes/shares", json={"agent_id": appender["id"]})
    assert shared.status_code == 201, shared.text
    trigger = api.post(
        "/api/triggers",
        json={
            "kind": "event",
            "event_type": "table_written",
            "target_kind": "agent",
            "target_id": appender["id"],
            "filter": {"tables": ["notes"]},
        },
    )
    assert trigger.status_code == 201, trigger.text

    def dispatch() -> None:
        dispatcher.drain_once(store.id)
        asyncio.run(executor.drain_once())

    say(api, chat, f"{mention} {scenarios.AGAIN}: buy eggs", ("agent", notes_agent))
    # The core loop's note fires the trigger, once: its agent appends a note of its own.
    dispatch()
    assert notes(store) == [
        "@Notes buy milk",
        f"@Notes {scenarios.AGAIN}: buy eggs",
        scenarios.APPENDED_NOTE,
    ]
    # That note is its echo: its own trigger refuses it.
    dispatch()
    after = api.get(f"/api/triggers/{trigger.json()['id']}").json()
    assert after["last_status"] == "refused" and "own target" in after["last_error"]
    assert len(api.get(f"/api/agents/{appender['id']}/runs").json()) == 1
    assert len(notes(store)) == 3


def test_a_turn_waiting_on_its_request_isnt_failed_or_requeued(
    api, notes_agent, scripted, queued, monkeypatch, run_service
):
    chat = make_chat(api, "Notes")
    add(api, chat, "agent", notes_agent)
    say(api, chat, f"@[Notes](agent:{notes_agent['id']}) buy milk", ("agent", notes_agent))

    # Nothing on the run service holds a run waiting for a person, and the sweep for lost work
    # leaves it be, however long it waits.
    monkeypatch.setattr(get_settings(), "queue_requeue_after_seconds", 0.001)
    assert sweep.requeue() == 0
    run_service.drain()

    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "running"
    assert invocation["agent_run"]["waiting"] is True


def test_a_turn_asked_to_take_its_time_survives_a_run_service_restart(
    api, notes_agent, scripted, monkeypatch, run_service
):
    """The full-stack test's second story, here: the run service stops mid-reply (its grace
    gone), the turn is handed back, and a new process carries it on to its reply."""
    monkeypatch.setattr(scenarios, "SLOW_SECONDS", 1.0)
    chat = make_chat(api, "Notes")
    add(api, chat, "agent", notes_agent)
    run_service.auto = False
    say(api, chat, f"@[Notes](agent:{notes_agent['id']}) write this slowly", ("agent", notes_agent))

    async def restarted_mid_turn() -> None:
        process = executor.Executor()
        for _ in range(100):
            await process.claim()
            await asyncio.sleep(0.05)
            [invocation] = messages(api, chat)["invocations"]
            if invocation["status"] == "running":
                break
        await asyncio.sleep(0.3)  # mid-call
        await process.stop(grace=0)

    asyncio.run(restarted_mid_turn())
    assert replies_from(api, chat) == []
    run_service.drain()

    assert replies_from(api, chat) == [("Notes", scenarios.WRITTEN_SLOWLY)]
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == ("succeeded", None)


@pytest.fixture
def published_drafts(monkeypatch) -> list[str]:
    """Each draft a chat following a turn is sent (`aios.streams.Draft`), as it's published."""
    monkeypatch.setattr(get_settings(), "draft_publish_interval_seconds", 0)
    published = []
    flush = streams.Draft.flush

    def recording(draft):
        if draft.text and draft.text != draft._published:
            published.append(draft.text)
        flush(draft)

    monkeypatch.setattr(streams.Draft, "flush", recording)
    return published


def test_chat_with_an_agent_told_to_write_as_it_goes_streams_its_reply_in_pieces(
    api, store, scripted, published_drafts, monkeypatch
):
    """The full-stack test's **Chat with this agent** story, here: the agent's reply is written
    a piece at a time, each a draft the chat is sent, then posted whole."""
    monkeypatch.setattr(scenarios, "PIECE_SECONDS", 0)
    agent = api.post(
        "/api/agents", json={"name": "Researcher", "base_prompt": scenarios.AS_IT_GOES}
    ).json()
    chat = api.post("/api/chats", json={"agent_id": agent["id"]})
    assert chat.status_code == 201, chat.text

    say(api, chat.json(), f"@[Researcher](agent:{agent['id']}) find papers", ("agent", agent))

    whole = "".join(scenarios.PIECES)
    assert replies_from(api, chat.json()) == [("Researcher", whole)]
    pieces = [scenarios.PIECES[0]]
    for piece in scenarios.PIECES[1:]:
        pieces.append(pieces[-1] + piece)
    assert published_drafts == pieces


def test_a_mentioned_workflows_json_result_is_handed_back_in_short_with_the_full_output_kept(
    api,
    store,
    scripted,
    queued_runs,  # noqa: F811
):
    """The full-stack test's handback story, here: a workflow whose agent replies in JSON is
    mentioned; its result is posted as the scripted handback's short summary of it, and the
    output it summarised is kept whole on the invocation."""
    output = '{"papers": ["A", "B", "C"], "found": 3}'
    agent = api.post(
        "/api/agents", json={"name": "Lister", "base_prompt": f"{scenarios.REPLY_WITH}{output}"}
    ).json()
    workflow = make_workflow(api, "Survey", [agent_node("list", agent)])
    chat = make_chat(api, "Reading")
    add(api, chat, "workflow", workflow)

    say_workflow(api, chat, f"@[Survey](workflow:{workflow['id']}) list them", workflow)
    drain_run(queued_runs.store, queued_runs[0])

    summary = "In short: 2 fields, papers and found."
    assert replies_from(api, chat, "workflow") == [("Survey", summary)]
    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "succeeded"
    assert invocation["full_output"] == output
    assert invocation["handback_error"] is None


@pytest.mark.parametrize(
    ("output", "summary"),
    [
        ('{"total": 3}', "In short: 1 field, total."),
        ('{"a": 1, "b": 2, "c": 3}', "In short: 3 fields, a, b and c."),
        ('["x", "y"]', "In short: 2 items."),
        ("{}", "{}"),
        ("Paid.", "Paid."),
        ("42", "42"),
    ],
)
def test_the_scripted_handback_shortens_json_and_passes_anything_else_as_it_came(output, summary):
    assert scenarios.handed_back(output) == summary


def test_embeddings_are_a_deterministic_fake(scripted):
    first, again, other = asyncio.run(
        embeddings.embed_documents(["buy milk", "buy milk", "buy bread"])
    )
    assert first == again != other
    assert len(first) == 1024
    assert asyncio.run(embeddings.embed_query("buy milk")) == first


def test_with_scripted_models_off_naming_one_fails_the_turn_as_an_unknown_provider(
    api, notes_agent, no_providers, monkeypatch
):
    monkeypatch.setattr(get_settings(), "llm_model", "scripted:core-loop")
    chat = make_chat(api, "Notes")
    add(api, chat, "agent", notes_agent)

    say(api, chat, f"@[Notes](agent:{notes_agent['id']}) buy milk", ("agent", notes_agent))

    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "failed"
    assert "Unknown model provider 'scripted'" in invocation["error"]
    with pytest.raises(ValueError, match="Unknown model provider 'scripted'"):
        llm.chat_model("scripted:core-loop")


def test_an_unknown_scenario_is_refused(scripted):
    with pytest.raises(ValueError, match="Unknown scripted scenario 'nope'"):
        llm.chat_model("scripted:nope")


def test_the_api_refuses_to_start_with_scripted_models_and_secure_cookies(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "scripted_models_enabled", True)
    monkeypatch.setattr(settings, "cookie_secure", True)
    with pytest.raises(RuntimeError, match="SCRIPTED_MODELS_ENABLED"):
        create_app()

    monkeypatch.setattr(settings, "cookie_secure", False)
    create_app()


# --- The inflow story (frontend/fullstack/inflow.spec.ts) ----------------------------------------


@pytest.fixture
def inflow(api, store, scripted, monkeypatch, run_service):
    """A test connector syncing a channel with one item about zebrafish into the organisation's
    store; the organisation's assistant; a chat it's in (a default chat agent)."""
    from test_connectors import connect, scoped
    from test_sync import run

    from aios.source_providers import test as provider

    monkeypatch.setattr(get_settings(), "sources_test_provider_enabled", True)
    channel = provider.feed(f"inflow-{store.id.hex[:8]}")
    channel.message("zebrafish", "Zebrafish embryos are transparent.", title="Zebrafish")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    run(store.id, source_id)
    [assistant] = [
        a for a in api.get("/api/agents").json() if a["name"] == "Organisation assistant"
    ]
    chat = make_chat(api, "Inflow")
    yield {
        "channel": channel,
        "connector_id": connector_id,
        "source_id": source_id,
        "assistant": assistant,
        "chat": chat,
        "sync": lambda: run(store.id, source_id),
    }
    provider.forget(channel.name)


def ask_assistant(api, inflow, question: str) -> str:
    assistant = inflow["assistant"]
    say(
        api,
        inflow["chat"],
        f"@[{assistant['name']}](agent:{assistant['id']}) {question}",
        ("agent", assistant),
    )
    return replies_from(api, inflow["chat"])[-1]


def test_the_assistant_answers_from_a_synced_item_and_says_so_once_its_removed_upstream(
    api, inflow
):
    """The full-stack test's inflow story, its assistant's part: it searches, and answers from
    the synced item, citing it; deleted upstream, the next sync removes it, and the assistant
    finds nothing to answer from."""
    assert ask_assistant(api, inflow, "what about zebrafish?") == (
        "Organisation assistant",
        "Zebrafish embryos are transparent. [Zebrafish]",
    )

    inflow["channel"].delete("zebrafish")
    assert inflow["sync"]().removed == 1

    assert ask_assistant(api, inflow, "what about zebrafish?") == (
        "Organisation assistant",
        scenarios.NOTHING_FOUND,
    )


def test_an_agent_told_to_post_to_a_channel_posts_with_the_connectors_tool(api, inflow):
    """The full-stack test's inflow story, its connector tool's part: an agent given the test
    connector's `post_message` posts what it was asked to the channel its instructions name."""
    channel = inflow["channel"]
    tools = [
        {
            "key": "connector",
            "config": {
                "connectors": [
                    {
                        "connector_id": inflow["connector_id"],
                        "provider": "test",
                        "tools": ["post_message"],
                    }
                ]
            },
        }
    ]
    poster = api.post(
        "/api/agents",
        json={
            "name": "Poster",
            "base_prompt": f"{scenarios.POST_TO}{channel.name}",
            "tools": tools,
        },
    )
    assert poster.status_code == 201, poster.text
    poster = poster.json()
    add(api, inflow["chat"], "agent", poster)

    say(
        api, inflow["chat"], f"@[Poster](agent:{poster['id']}) the tank is clean", ("agent", poster)
    )

    assert replies_from(api, inflow["chat"])[-1] == ("Poster", scenarios.POSTED)
    assert [e.content.text for e in channel.items.values()][-1] == "@Poster the tank is clean"
    [entry] = api.get("/api/audit-log", params={"action": "connector.tool_called"}).json()[
        "entries"
    ]
    assert (entry["details"]["tool"], entry["details"]["target"]) == ("post_message", channel.name)
