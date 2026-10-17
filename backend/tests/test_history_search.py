"""Searchable run history: embedding the exported messages (a second setting of the run history
export, on the Settings page) and searching past runs with hybrid retrieval
(`aios.datastores.history`, `aios.search`)."""

import math
import re

import pytest
from fake_model import ToolCall, model, use_model
from test_agents import agent_body, chat_body, make_agent
from test_history_export import chat, new_run, set_up_and_export, settings, stored, switch

from aios import search, worker
from aios.datastores import data_store_for, history

DIM = 1024

# The fake embedder's meanings: words with the same axis mean much the same.
MEANINGS = {"cat": 0, "cats": 0, "feline": 0, "felines": 0, "kitten": 0, "kittens": 0, "purr": 0}
for word in ("zebrafish", "fish", "aquarium"):
    MEANINGS[word] = 1
for word in ("sourdough", "bread", "loaf", "baking"):
    MEANINGS[word] = 2


def meaning(text: str) -> list[float]:
    """A unit vector for `text`: the sum of its words' meanings (the last axis if none)."""
    v = [0.0] * DIM
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in MEANINGS:
            v[MEANINGS[word]] += 1.0
    if not any(v):
        v[DIM - 1] = 1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


@pytest.fixture
def embedded(monkeypatch) -> list[str]:
    """Texts the export embedded, with `meaning` instead of the embedding API."""
    texts: list[str] = []

    async def embed_documents(batch):
        texts.extend(batch)
        return [meaning(t) for t in batch]

    monkeypatch.setattr(history, "embed_documents", embed_documents)
    return texts


def embed(api, on: bool):
    return api.put("/api/settings", json={"embed_run_history": on})


@pytest.fixture
def store_id(user):
    return data_store_for(user.id)


def embedded_messages(store_id) -> list[tuple]:
    return stored(
        store_id,
        "SELECT role, text, embedding IS NOT NULL FROM history.messages"
        " ORDER BY agent_run_id, position",
    )


def test_embedding_is_off_by_default_and_offered_only_while_the_export_is_on(api):
    assert settings(api)["embed_run_history"] is False
    assert settings(api)["history_embedded_at"] is None

    # Not while the export is off.
    response = embed(api, True)
    assert response.status_code == 409
    assert settings(api)["embed_run_history"] is False

    switch(api, True)
    response = embed(api, True)
    assert response.status_code == 200
    assert response.json()["embed_run_history"] is True
    assert response.json()["export_run_history"] is True
    assert settings(api)["embed_run_history"] is True

    # Switching the export off switches embedding off with it.
    assert switch(api, False)["embed_run_history"] is False
    switch(api, True)
    assert settings(api)["embed_run_history"] is False


def test_with_embedding_on_messages_are_embedded_as_theyre_exported(
    api, store_id, history_tasks, embedded, monkeypatch
):
    switch(api, True)
    embed(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    tools = [{"key": "hybrid_search", "config": {}}]
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=tools)).json()
    run = new_run(api, agent)

    async def embed_query(text):
        return meaning(text)

    monkeypatch.setattr(search, "embed_query", embed_query)

    async def uses_a_tool(messages, info):
        if messages[-1].type == "tool":
            yield "Cats purr when content."
        else:
            yield ToolCall("search_documents", {"query": "cats"})

    with use_model(model(uses_a_tool)):
        api.post(
            f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("Why do cats purr?")
        )
    history.export(store_id)

    # The user's and the assistant's words: not tool results, nor empty text.
    rows = embedded_messages(store_id)
    assert [(role, done) for role, _, done in rows] == [
        ("user", True),
        ("assistant", False),
        ("tool", False),
        ("assistant", True),
    ]
    assert embedded == ["Why do cats purr?", "Cats purr when content."]

    # Exporting the run again embeds only what's new.
    chat(api, agent, run, "And kittens?", "Kittens purr too.")
    history.export(store_id)
    assert embedded[2:] == ["And kittens?", "Kittens purr too."]
    assert settings(api)["history_embedded_at"] is not None


def test_switching_it_on_backfills_and_switching_it_off_keeps_what_was_embedded(
    api, store_id, history_tasks, embedded
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "Tell me about sourdough", "A loaf needs a starter.")
    history.export(store_id)
    assert [done for _, _, done in embedded_messages(store_id)] == [False, False]
    history_tasks["export"].clear()

    assert embed(api, True).json()["history_embedded_at"] is None
    # The backfill: the store's export is queued, and embeds what's there.
    assert history_tasks["export"] == [store_id]
    worker.export_history_task.apply(args=[str(store_id)]).get()
    assert [done for _, _, done in embedded_messages(store_id)] == [True, True]
    assert settings(api)["history_embedded_at"] is not None

    embed(api, False)
    chat(api, agent, run, "And rye bread?", "Rye is denser.")
    history.export(store_id)
    # Nothing new is embedded; what was is kept.
    assert [done for _, _, done in embedded_messages(store_id)] == [True, True, False, False]
    assert embedded == ["Tell me about sourdough", "A loaf needs a starter."]


async def _meanings(batch):
    return [meaning(t) for t in batch]


def test_a_backfill_left_unfinished_is_picked_up_by_the_sweep(
    api, store_id, history_tasks, embedded, monkeypatch
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "Cats", "Felines")
    history.export(store_id)
    assert history.stores_to_export() == []

    async def down(batch):
        raise RuntimeError("embedding API is down")

    monkeypatch.setattr(history, "embed_documents", down)
    embed(api, True)
    history.export(store_id)
    assert settings(api)["history_embedded_at"] is None
    assert store_id in history.stores_to_export()

    monkeypatch.setattr(history, "embed_documents", _meanings)
    history.export(store_id)
    assert [done for _, _, done in embedded_messages(store_id)] == [True, True]
    assert store_id not in history.stores_to_export()
