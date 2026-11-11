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
from conftest import store_of
from pydantic import SecretStr
from test_chat_agent_invocations import age, replies_from, say
from test_chats import add, make_chat, messages
from test_data_tools import as_owner, provisioned
from test_schema_change_requests import run_queued

from aios import embeddings, handoff, llm, tracing
from aios.api import chats as chats_api
from aios.config import get_settings
from aios.main import create_app
from aios.models import DataStore
from aios.testing import scenarios

# The tasks the code queued (applying a change, resuming a run), to run as the workers would.
queued = test_schema_change_requests.queued

# The handoff's own model, as the app has it: every test's is scripted by default (conftest).
APP_HANDOFF_MODEL = handoff.model

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
    handoff on it too (the deployment's), with Langfuse configured as if for real."""
    settings = get_settings()
    monkeypatch.setattr(settings, "scripted_models_enabled", True)
    monkeypatch.setattr(settings, "llm_model", "scripted:core-loop")
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", SecretStr("sk-lf-test"))
    with mock.patch.object(handoff, "model", APP_HANDOFF_MODEL):
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
    [request] = api.get("/api/schema-changes").json()
    assert (request["status"], request["summary"]) == ("pending", "Create table data.notes")

    # Approved (as on the Data page), it's applied and the run resumes, writes and replies.
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 200
    run_queued(queued)

    assert notes(store) == ["@Notes buy milk"]
    assert replies_from(api, chat) == [("Notes", scenarios.SAVED)]
    [invocation] = messages(api, chat, after=card["id"])["invocations"]
    assert invocation["status"] == "succeeded"
    assert invocation["agent_run"]["waiting"] is False


def test_a_turn_waiting_on_its_request_isnt_failed_as_stopped(
    api, db, notes_agent, scripted, queued, in_store
):
    chat = make_chat(api, "Notes")
    add(api, chat, "agent", notes_agent)
    say(api, chat, f"@[Notes](agent:{notes_agent['id']}) buy milk", ("agent", notes_agent))
    [invocation] = messages(api, chat)["invocations"]

    # No process beats a run waiting for a person.
    age(db, in_store, invocation["id"])
    chats_api.fail_stopped_turns(db)

    [invocation] = messages(api, chat)["invocations"]
    assert invocation["status"] == "running"
    assert invocation["agent_run"]["waiting"] is True


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
