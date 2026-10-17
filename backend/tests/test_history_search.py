"""Searchable run history: embedding the exported messages (a second setting of the run history
export, on the Settings page) and searching past runs with hybrid retrieval
(`aios.datastores.history`, `aios.search`)."""

import asyncio
import math
import re

import pytest
from conftest import delete_user, provisioned_user
from fake_model import ToolCall, last_tool_results, model, replies, tool_output, use_model
from sqlalchemy import func, text, update
from test_agents import agent_body, chat_body, make_agent
from test_history_export import chat, new_run, set_up_and_export, settings, stored, switch

from aios import search, worker
from aios.auth import current_user
from aios.datastores import (
    DataStoreNotReady,
    data_store_for,
    history,
    store_engine,
    store_session,
)
from aios.datastores.connections import dispose_engines
from aios.db import SessionLocal
from aios.main import app
from aios.models import AgentRun, DataStore, DataStoreStatus

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


def test_a_backfill_out_of_time_is_picked_up_by_the_sweep(
    api, store_id, history_tasks, embedded, monkeypatch
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "Cats", "Felines")
    history.export(store_id)
    assert history.stores_to_export() == []

    # One message per batch, and no time for a second.
    monkeypatch.setattr(history, "EMBED_BATCH", 1)
    monkeypatch.setattr(history, "EMBED_BUDGET", 0)
    embed(api, True)
    history.export(store_id)
    assert [done for _, _, done in embedded_messages(store_id)] == [True, False]
    assert settings(api)["history_embedded_at"] is None
    assert store_id in history.stores_to_export()

    monkeypatch.setattr(history, "EMBED_BATCH", 128)
    history.export(store_id)
    assert [done for _, _, done in embedded_messages(store_id)] == [True, True]
    assert store_id not in history.stores_to_export()


def test_embedding_that_fails_says_why_and_is_tried_again_later(
    api, store_id, history_tasks, embedded, monkeypatch
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "Cats", "Felines")
    history.export(store_id)

    async def down(batch):
        raise RuntimeError("embedding API is down")

    monkeypatch.setattr(history, "embed_documents", down)
    embed(api, True)
    history.export(store_id)
    assert settings(api)["history_embedded_at"] is None
    assert settings(api)["history_embed_error"] == "RuntimeError: embedding API is down"
    # Not every minute: after `EMBED_RETRY_AFTER`.
    assert store_id not in history.stores_to_export()
    with SessionLocal() as db:
        db.execute(
            update(DataStore)
            .where(DataStore.id == store_id)
            .values(history_embed_retry_at=func.now())
        )
        db.commit()
    assert store_id in history.stores_to_export()

    monkeypatch.setattr(history, "embed_documents", _meanings)
    history.export(store_id)
    assert [done for _, _, done in embedded_messages(store_id)] == [True, True]
    assert settings(api)["history_embed_error"] is None
    assert store_id not in history.stores_to_export()


def test_deleting_exported_history_embeds_it_again_as_its_exported_again(
    api, store_id, history_tasks, embedded
):
    switch(api, True)
    embed(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "Cats", "Felines")
    history.export(store_id)
    assert settings(api)["history_embedded_at"] is not None

    assert api.delete("/api/settings/exported-history").status_code == 204

    # Not caught up until what's exported again is embedded again.
    assert settings(api)["history_embedded_at"] is None
    history.export(store_id)
    assert [done for _, _, done in embedded_messages(store_id)] == [True, True]
    assert settings(api)["history_embedded_at"] is not None


def test_a_long_message_is_embedded_by_its_start(api, store_id, history_tasks, embedded):
    """So a batch stays inside the embedding API's limit on tokens per request."""
    switch(api, True)
    embed(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    long = "cats " * history.EMBED_MAX_CHARS
    chat(api, agent, new_run(api, agent), long, "ok")
    history.export(store_id)

    assert embedded == [long[: history.EMBED_MAX_CHARS], "ok"]


HISTORY_TOOL = {"key": "history_search", "config": {"max_results": 3}}


def search_history_once(query: str):
    """A model that searches past runs once with `query`, then replies with what came back."""
    seen = {}

    async def script(messages, info):
        if returns := last_tool_results(messages):
            seen["results"] = tool_output(returns[0])
            yield f"found {len(seen['results'])}"
        else:
            seen["description"] = info.tools["search_history"].description
            yield ToolCall("search_history", {"query": query})

    return model(script), seen


def ask(api, agent: dict, run: dict, query: str) -> dict:
    fake, seen = search_history_once(query)
    with use_model(fake):
        response = api.post(
            f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("remember?")
        )
    assert response.status_code == 200
    return seen


@pytest.fixture
def query_meaning(monkeypatch):
    async def embed_query(text):
        return meaning(text)

    monkeypatch.setattr(search, "embed_query", embed_query)


@pytest.fixture
def past_runs(api, store_id, history_tasks, embedded) -> dict:
    """Two earlier runs, exported and embedded: one about baking, one about cats."""
    switch(api, True)
    embed(api, True)
    set_up_and_export(store_id, history_tasks)
    baker = make_agent(api, "Baker")
    baking = new_run(api, baker)
    chat(api, baker, baking, "How do I keep my sourdough going?", "Feed the starter daily.")
    cats = new_run(api, baker)
    chat(api, baker, cats, "Why do felines knead?", "It comforts them, Kneading-wise.")
    history.export(store_id)
    return {"baker": baker, "baking": baking, "cats": cats}


def test_an_agent_finds_a_passage_from_an_earlier_run_by_meaning_and_by_keyword(
    api, past_runs, query_meaning
):
    agent = make_agent(api, "Rememberer")
    agent = api.put(
        f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[HISTORY_TOOL])
    ).json()
    run = new_run(api, agent)
    baking = past_runs["baking"]
    with SessionLocal() as db:
        started = db.get(AgentRun, baking["id"]).created_at

    # By meaning: "bread" shares no word with the run, only its meaning.
    seen = ask(api, agent, run, "bread")
    assert "past runs" in seen["description"]
    assert seen["results"][0] == {
        "run_id": baking["id"],
        "agent": "Baker",
        "title": "How do I keep my sourdough going?",
        "when": started.isoformat(),
        "role": "user",
        "text": "How do I keep my sourdough going?",
    }
    assert len(seen["results"]) == 3

    # By keyword: "starter" means nothing to the embedder, but it's a word in the run.
    seen = ask(api, agent, new_run(api, agent), "starter")
    assert seen["results"][0]["text"] == "Feed the starter daily."
    assert seen["results"][0]["run_id"] == baking["id"]


def test_searching_history_leaves_out_the_run_searching(api, store_id, past_runs, query_meaning):
    agent = make_agent(api, "Rememberer")
    agent = api.put(
        f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[HISTORY_TOOL])
    ).json()
    run = new_run(api, agent)
    chat(api, agent, run, "Kittens and sourdough", "Both need feeding.")
    history.export(store_id)

    seen = ask(api, agent, run, "kittens sourdough")

    assert run["id"] not in {r["run_id"] for r in seen["results"]}
    assert {r["run_id"] for r in seen["results"]} == {
        past_runs["baking"]["id"],
        past_runs["cats"]["id"],
    }


def test_searching_history_never_returns_another_stores_runs(
    db, api, user, past_runs, query_meaning, history_tasks
):
    other = provisioned_user(db, -2, "other-user")
    try:
        app.dependency_overrides[current_user] = lambda: other
        other_store = data_store_for(other.id)
        switch(api, True)
        embed(api, True)
        set_up_and_export(other_store, history_tasks)
        agent = make_agent(api, "Theirs")
        agent = api.put(
            f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[HISTORY_TOOL])
        ).json()
        chat(api, agent, new_run(api, agent), "My zebrafish aquarium", "Lovely fish.")
        history.export(other_store)

        seen = ask(api, agent, new_run(api, agent), "sourdough starter felines")
        assert {r["agent"] for r in seen["results"]} == {"Theirs"}

        app.dependency_overrides[current_user] = lambda: user
        mine = make_agent(api, "Mine")
        mine = api.put(
            f"/api/agents/{mine['id']}", json=agent_body(mine, tools=[HISTORY_TOOL])
        ).json()
        seen = ask(api, mine, new_run(api, mine), "zebrafish aquarium")
        assert {r["agent"] for r in seen["results"]} == {"Baker"}
    finally:
        app.dependency_overrides[current_user] = lambda: user
        delete_user(db, other)


def test_searching_history_before_any_was_exported_says_so(api, query_meaning):
    agent = make_agent(api, "Rememberer")
    agent = api.put(
        f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[HISTORY_TOOL])
    ).json()

    seen = ask(api, agent, new_run(api, agent), "sourdough")

    assert seen["results"] == [{"error": search.NO_HISTORY}]


def test_with_the_embedding_api_down_history_is_still_searched_by_keyword(
    store_id, past_runs, monkeypatch, caplog
):
    async def down(text):
        raise RuntimeError("embedding API is down")

    monkeypatch.setattr(search, "embed_query", down)

    results = asyncio.run(search.history_search_results(data_store_id=store_id, query="starter"))

    assert [r["text"] for r in results] == ["Feed the starter daily."]
    assert "Couldn't embed the history search's query" in caplog.text


def test_searching_history_behind_its_lines_head_says_to_try_again(
    api, store_id, past_runs, query_meaning
):
    # As while a deploy brings the store to h0003, which gives the messages their search columns.
    with SessionLocal() as db:
        db.execute(
            update(DataStore).where(DataStore.id == store_id).values(history_revision="h0002")
        )
        db.commit()

    results = asyncio.run(search.history_search_results(data_store_id=store_id, query="bread"))

    assert results == [{"error": search.STORE_UPGRADING}]


@pytest.fixture
def exported_unembedded(api, store_id, history_tasks):
    """The export on, a run exported, and embedding switched on but not yet run."""
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "Cats", "Felines")
    history.export(store_id)
    embed(api, True)


def test_embedding_holds_no_store_session_while_the_embedding_api_answers(
    store_id, exported_unembedded, monkeypatch
):
    """As for search (test_search_opens_its_session_after_embedding): the store's engine may be
    evicted, and disposed of, meanwhile."""
    evicted = store_engine(store_id, "rw")

    async def embed_documents(batch):
        dispose_engines()  # as `MAX_ENGINES` newer engines would
        return [meaning(t) for t in batch]

    monkeypatch.setattr(history, "embed_documents", embed_documents)
    history.export(store_id)

    assert [done for _, _, done in embedded_messages(store_id)] == [True, True]
    assert evicted.pool.checkedin() == 0


def test_a_store_that_goes_away_while_embedding_isnt_logged_as_an_embedding_failure(
    store_id, exported_unembedded, monkeypatch, caplog
):
    """The export task handles a store that's gone (or going), or behind: `_embed` mustn't
    swallow it."""

    async def embed_documents(batch):
        with SessionLocal() as db:
            db.execute(
                update(DataStore)
                .where(DataStore.id == store_id)
                .values(status=DataStoreStatus.deleting)
            )
            db.commit()
        return [meaning(t) for t in batch]

    monkeypatch.setattr(history, "embed_documents", embed_documents)
    with pytest.raises(DataStoreNotReady):
        history.export(store_id)
    assert "Couldn't embed" not in caplog.text


def test_embedding_holds_up_neither_exports_nor_deleting_exported_history(
    api, store_id, exported_unembedded, monkeypatch
):
    """A backfill can embed for minutes: the export lock isn't held meanwhile."""
    monkeypatch.setattr(history, "LOCK_WAIT", 0.2)
    agent = make_agent(api, "Other")
    run = new_run(api, agent)
    with use_model(replies("ok")):
        api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("later"))
    during = {}

    async def embed_documents(batch):
        # A run changed meanwhile: its export goes in.
        with SessionLocal() as db:
            history.enqueue_runs(db, AgentRun.id == run["id"])
            db.commit()
        during["exported"] = history.export(store_id)
        history.delete_exported(store_id)
        return [meaning(t) for t in batch]

    monkeypatch.setattr(history, "embed_documents", embed_documents)
    history.export(store_id)

    assert during["exported"] == 1
    # Deleted under it: what it embedded is gone with the rows.
    assert stored(store_id, "SELECT count(*) FROM history.messages") == [(0,)]


def test_a_message_whose_text_changed_while_embedding_isnt_given_the_old_texts_embedding(
    store_id, exported_unembedded, monkeypatch
):
    async def embed_documents(batch):
        with store_session(store_id, "rw") as store:
            store.execute(text("UPDATE history.messages SET text = 'Sourdough' WHERE position = 1"))
            store.commit()
        return [meaning(t) for t in batch]

    monkeypatch.setattr(history, "embed_documents", embed_documents)
    history.export(store_id)
    monkeypatch.setattr(history, "embed_documents", _meanings)

    # Not "Felines"' embedding: it's waiting for one of its own, which the next pass gives it.
    assert stored(store_id, "SELECT text, embedding IS NULL FROM history.messages ORDER BY 1") == [
        ("Cats", False),
        ("Sourdough", True),
    ]
    history.export(store_id)
    assert stored(
        store_id,
        "SELECT embedding = CAST(:v AS vector) FROM history.messages WHERE position = 1".replace(
            ":v", "'" + search.vector_literal(meaning("Sourdough")) + "'"
        ),
    ) == [(True,)]
