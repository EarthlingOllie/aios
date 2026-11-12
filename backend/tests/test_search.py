"""Unified search (`aios.search`, `aios.search_index`): one index in the organisation's store of its
documents' chunks, chat messages, past runs' messages and briefs, written with them and embedded
in the background, searched by the Search page's endpoint, the `search` tool, and the document
search and history search tools (the same search, fixed to their kind)."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import asyncio
import math
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    build_user,
    delete_user,
    join_organisation,
    org_of,
    provisioned_user,
    store_of,
    take_turn,
)
from sqlalchemy import delete, func, select, text, update
from test_agents import agent_body, make_agent
from test_chats import add, make_chat, messages, post, signed_in_as

from aios import ingest, search, search_index
from aios.auth import current_data_store, current_user
from aios.config import get_settings
from aios.datastores import data_store_for, store_engine, store_session
from aios.datastores.connections import dispose_engines
from aios.datastores.models import Chunk, Document, DocumentStatus, SearchItem
from aios.db import SessionLocal
from aios.jev import Verdict
from aios.main import app
from aios.models import Role, User, WorkItem, WorkKind, WorkLane
from aios.permissions import current_role
from aios.run_service import executor, queue, sweep, work
from aios.search import Filters, search_with_vector
from aios.sources import uploads_source_id
from aios.testing.fake_model import (
    ToolCall,
    last_tool_results,
    model,
    replies,
    tool_output,
    use_handoff,
    use_model,
)

DIM = 1024


def unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def blend(a: int, b: int) -> list[float]:
    v = [0.0] * DIM
    v[a] = v[b] = 1 / math.sqrt(2)
    return v


def add_chunks(
    store_id: uuid.UUID, filename: str, chunks: list[tuple[str, list[float]]], **meta
) -> int:
    """A ready document in the store, of these chunks (text, embedding), indexed as ingest
    indexes them once Jev has passed them, and embedded as the sweep would embed them. Its id."""
    doc = Document(
        filename=filename,
        s3_key="",
        size_bytes=0,
        sha256="",
        status=DocumentStatus.ready,
        verdict=Verdict.clean,
    )
    doc.chunks = [
        Chunk(ordinal=i, text=t, contextualized=t, meta=meta) for i, (t, _) in enumerate(chunks)
    ]
    with store_session(store_id, "rw") as store:
        doc.source_id = uploads_source_id(store)
        store.add(doc)
        store.flush()
        search_index.index_chunks(store, doc.id)
        for chunk, (_, vec) in zip(doc.chunks, chunks, strict=True):
            store.execute(
                text(
                    "UPDATE aios.search_items SET embedding = CAST(:v AS vector)"
                    " WHERE chunk_id = :c"
                ),
                {"v": search.vector_literal(vec), "c": chunk.id},
            )
        store.commit()
        return doc.id


# The fake embedder's meanings: words with the same axis mean much the same.
MEANINGS = {"cat": 0, "cats": 0, "feline": 0, "felines": 0, "kitten": 0, "kittens": 0, "purr": 0}
for word in ("zebrafish", "fish", "aquarium"):
    MEANINGS[word] = 1
for word in ("sourdough", "bread", "loaf", "baking"):
    MEANINGS[word] = 2


def meaning(words: str) -> list[float]:
    """A unit vector for `words`: the sum of its words' meanings (the last axis if none)."""
    v = [0.0] * DIM
    for word in re.findall(r"[a-z]+", words.lower()):
        if word in MEANINGS:
            v[MEANINGS[word]] += 1.0
    if not any(v):
        v[DIM - 1] = 1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


@pytest.fixture
def embedded(monkeypatch) -> list[str]:
    """Texts the sweep sent the embedding API (`meaning` instead of it), and queries embedded
    by `meaning` too."""
    texts: list[str] = []

    async def embed_documents(batch):
        texts.extend(batch)
        return [meaning(t) for t in batch]

    async def embed_query(query):
        return meaning(query)

    monkeypatch.setattr(search_index, "embed_documents", embed_documents)
    monkeypatch.setattr(search, "embed_query", embed_query)
    return texts


@pytest.fixture
def store_id(user) -> uuid.UUID:
    return store_of(user)


@pytest.fixture
def member(db, user) -> User:
    """A second member of `user`'s organisation."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    delete_user(db, u)


def found(api, q: str, *kinds: str, limit: int | None = None) -> list[dict]:
    params: dict = {"q": q, "kind": list(kinds)}
    if limit is not None:
        params["limit"] = limit
    response = api.get("/api/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def chat_turn(api, agent: dict, run: dict, message: str, reply: str = "ok") -> None:
    with use_model(replies(reply)):
        take_turn(api, agent["id"], run["id"], message)


def new_run(api, agent: dict) -> dict:
    return api.post(f"/api/agents/{agent['id']}/runs").json()


# --- Ranking -------------------------------------------------------------------------------------


def test_search_fuses_semantic_and_lexical(user):
    store_id = store_of(user)
    texts = {
        "semantic": ("Cats are small domesticated felines.", unit(0)),
        "lexical": ("The zebrafish genome was sequenced in 2013.", unit(1)),
        "both": ("Zebrafish are popular in genetics labs.", blend(0, 1)),
    }
    add_chunks(store_id, "notes.md", list(texts.values()))

    with store_session(store_id, "ro") as store:
        hits = search_with_vector(store, query="zebrafish", query_vector=unit(0), limit=5)
    by_text = {h.text: h for h in hits}

    semantic, lexical, both = (by_text[t] for t, _ in texts.values())

    # The keyword matches get a boost from the full-text branch...
    assert both.lexical_rank is not None and lexical.lexical_rank is not None
    assert semantic.lexical_rank is None
    # ...so chunks that rank in both lists beat the best pure vector match.
    assert both.score > semantic.score
    assert lexical.score > semantic.score


def test_search_never_returns_another_stores_items(db, user, monkeypatch):
    """Each store is a database of its own, which only its own roles may connect to
    (test_provisioning): a search runs in one store, so it can't see another's rows."""
    other = provisioned_user(db, "other-user")
    try:
        add_chunks(store_of(user), "mine.md", [("Zebrafish fins.", unit(0))])
        add_chunks(store_of(other), "theirs.md", [("Zebrafish genome.", unit(0))])

        async def embed_query(text):
            return unit(0)

        monkeypatch.setattr(search, "embed_query", embed_query)
        for owner, filename in ((user, "mine.md"), (other, "theirs.md")):
            hits = asyncio.run(search.search(data_store_id=store_of(owner), query="fish"))
            assert [h.filename for h in hits] == [filename]
    finally:
        delete_user(db, other)


def test_search_opens_its_session_after_embedding(user, monkeypatch):
    """The store's engine can be evicted (and disposed of) while the query is embedded: the
    search mustn't then open connections on it, in a pool nothing tracks or disposes of."""
    store_id = store_of(user)
    add_chunks(store_id, "notes.md", [("Zebrafish fins.", unit(0))])
    evicted = store_engine(store_id, "ro")

    async def embed_query(text):
        dispose_engines()  # as `MAX_ENGINES` newer engines would
        return unit(0)

    monkeypatch.setattr(search, "embed_query", embed_query)
    hits = asyncio.run(search.search(data_store_id=store_id, query="fish"))

    assert [h.filename for h in hits] == ["notes.md"]
    assert evicted.pool.checkedin() == 0


@pytest.mark.parametrize(
    "params",
    [
        {"q": ""},
        {"q": "   "},
        {"q": "x", "limit": 0},
        {"q": "x", "limit": -1},
        {"q": "x", "limit": 51},
        {"q": "x", "kind": "memory"},
    ],
)
def test_search_endpoint_rejects_bad_params(client, params):
    app.dependency_overrides[current_user] = lambda: build_user("t", id=1)
    app.dependency_overrides[current_data_store] = uuid.uuid4
    app.dependency_overrides[current_role] = lambda: Role.participant
    try:
        resp = client.get("/api/o/1/search", params=params)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_a_blank_query_skips_embedding():
    # Would hit the embedding API (and fail without a key) if not short-circuited.
    assert asyncio.run(search.search(data_store_id=uuid.uuid4(), query="  ")) == []


# --- One search over everything -----------------------------------------------------------------


@pytest.fixture
def everything(api, store_id, embedded) -> dict:
    """A document, a chat message and a past run's messages, all about zebrafish, embedded."""
    doc_id = add_chunks(store_id, "fish.md", [("Zebrafish are striped.", meaning("zebrafish"))])
    chat = make_chat(api, "Lab")
    post(api, chat, "The zebrafish tank needs cleaning.")
    agent = make_agent(api, "Keeper")
    run = new_run(api, agent)
    chat_turn(api, agent, run, "How often do zebrafish eat?", "Twice a day.")
    search_index.embed_pending(store_id)
    return {"doc_id": doc_id, "chat": chat, "agent": agent, "run": run}


def test_one_search_finds_a_document_a_chat_message_and_a_past_runs_message_with_their_sources(
    api, everything
):
    hits = found(api, "zebrafish")

    # The first (best) of each kind.
    by_kind = {h["kind"]: h for h in reversed(hits)}
    assert set(by_kind) == {"document_chunk", "chat_message", "run_message"}
    document, message, run_message = (
        by_kind[k] for k in ("document_chunk", "chat_message", "run_message")
    )
    assert (document["document_id"], document["filename"], document["text"]) == (
        everything["doc_id"],
        "fish.md",
        "Zebrafish are striped.",
    )
    chat = everything["chat"]
    [posted] = [m for m in messages(api, chat)["messages"] if m["kind"] == "text"]
    assert (message["chat_id"], message["chat_name"], message["message_id"]) == (
        chat["id"],
        "Lab",
        posted["id"],
    )
    assert (run_message["agent_run_id"], run_message["agent_id"], run_message["agent_name"]) == (
        everything["run"]["id"],
        everything["agent"]["id"],
        "Keeper",
    )
    assert (run_message["ref"]["role"], run_message["text"]) == (
        "user",
        "How often do zebrafish eat?",
    )
    # Each with how both branches ranked it, for the ranking explanation.
    assert all(h["semantic_rank"] is not None and h["score"] > 0 for h in hits)


def test_the_kind_filter_narrows_the_search_to_those_kinds(api, everything):
    assert {h["kind"] for h in found(api, "zebrafish", "chat_message")} == {"chat_message"}
    assert {h["kind"] for h in found(api, "zebrafish", "document_chunk", "run_message")} == {
        "document_chunk",
        "run_message",
    }
    assert len(found(api, "zebrafish", limit=1)) == 1


def test_a_turn_indexes_what_the_user_and_the_assistant_said_not_tool_results(
    api, store_id, embedded
):
    agent = make_agent(api, "Bot")
    tools = [{"key": "hybrid_search", "config": {}}]
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=tools)).json()
    run = new_run(api, agent)
    add_chunks(store_id, "cats.md", [("A tabby purrs loudly.", meaning("cats"))])

    async def uses_a_tool(msgs, info):
        if msgs[-1].type == "tool":
            yield "Cats purr when content."
        else:
            yield ToolCall("search_documents", {"query": "purr"})

    with use_model(model(uses_a_tool)):
        take_turn(api, agent["id"], run["id"], "Why do cats purr?")
    chat_turn(api, agent, run, "And kittens?", "Kittens purr too.")

    said = [(h["ref"]["role"], h["text"]) for h in found(api, "purr", "run_message", limit=50)]
    # The tool's result (the passage) isn't a run message; the turns' words are, once each.
    assert sorted(said) == [
        ("assistant", "Cats purr when content."),
        ("assistant", "Kittens purr too."),
        ("user", "Why do cats purr?"),
    ]
    assert "A tabby purrs loudly." not in {h["text"] for h in found(api, "tabby", "run_message")}


def test_the_search_page_shows_a_person_everything_in_the_organisation(
    api, user, member, store_id, embedded
):
    """Even a private chat they're not in: search items have no visibility."""
    with signed_in_as(member):
        secret = make_chat(api, "Members only")
        post(api, secret, "The sourdough starter lives in the fridge.")

    hits = found(api, "sourdough")
    assert [(h["kind"], h["chat_id"]) for h in hits] == [("chat_message", secret["id"])]


def test_deleting_a_source_takes_its_items_with_it(api, store_id, everything):
    agent, run = everything["agent"], everything["run"]
    assert api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}").status_code == 204
    with store_session(store_id, "rw") as store:
        # Nobody deletes a chat, but its messages' items go with it all the same.
        store.execute(text("DELETE FROM aios.chats"))
        store.execute(text("DELETE FROM aios.documents"))
        store.commit()

    assert found(api, "zebrafish") == []


# --- Embedding -----------------------------------------------------------------------------------


def test_with_embeddings_unavailable_search_is_by_keyword_and_a_later_sweep_embeds(
    api, store_id, monkeypatch, caplog
):
    async def down(_):
        raise RuntimeError("embedding API is down")

    monkeypatch.setattr(search, "embed_query", down)
    monkeypatch.setattr(search_index, "embed_documents", down)
    chat = make_chat(api, "Kitchen")
    post(api, chat, "Feed the sourdough starter daily.")
    # Writing never waits on the embedding API, and the sweep fails without losing anything.
    with pytest.raises(RuntimeError):
        search_index.embed_pending(store_id)

    hits = found(api, "starter")
    assert [(h["text"], h["semantic_rank"], h["lexical_rank"]) for h in hits] == [
        ("Feed the sourdough starter daily.", None, 1)
    ]
    assert "Couldn't embed the search's query" in caplog.text

    sent: list[str] = []

    async def embed_documents(batch):
        sent.extend(batch)
        return [meaning(t) for t in batch]

    async def embed_query(query):
        return meaning(query)

    monkeypatch.setattr(search_index, "embed_documents", embed_documents)
    monkeypatch.setattr(search, "embed_query", embed_query)
    assert search_index.embed_pending(store_id) == 1
    assert sent == ["Feed the sourdough starter daily."]
    # Now by meaning too: "bread" is no word of it.
    [hit] = found(api, "bread")
    assert (hit["text"], hit["semantic_rank"], hit["lexical_rank"]) == (
        "Feed the sourdough starter daily.",
        1,
        None,
    )


def test_unchanged_text_isnt_embedded_again(api, store_id, embedded):
    chat = make_chat(api, "Echo")
    post(api, chat, "Cats nap a lot.")
    search_index.embed_pending(store_id)
    assert embedded == ["Cats nap a lot."]

    # The same words again take the embedding they already have; nothing new, nothing sent.
    post(api, chat, "Cats nap a lot.")
    search_index.embed_pending(store_id)
    search_index.embed_pending(store_id)
    assert embedded == ["Cats nap a lot."]
    assert [h["semantic_rank"] is not None for h in found(api, "kittens")] == [True, True]


def test_an_item_written_again_keeps_its_embedding_only_while_its_text_is_unchanged(
    store_id, embedded
):
    doc_id = add_chunks(store_id, "a.md", [("Cats.", unit(0)), ("Fish.", unit(1))])
    with store_session(store_id, "rw") as store:
        # As if ingest wrote the chunks again: one unchanged, one changed.
        store.execute(
            text(
                "UPDATE aios.chunks SET text = 'Bread.', contextualized = 'Bread.'"
                " WHERE text = 'Fish.'"
            )
        )
        search_index.index_chunks(store, doc_id)
        store.commit()
        rows = dict(
            store.execute(
                select(SearchItem.text, SearchItem.embedding.is_(None)).order_by(SearchItem.id)
            ).all()
        )
    assert rows == {"Cats.": False, "Bread.": True}
    search_index.embed_pending(store_id)
    assert embedded == ["Bread."]


def test_the_sweep_embeds_under_a_budget_and_leaves_the_rest_for_the_next(
    api, store_id, embedded, monkeypatch
):
    chat = make_chat(api, "Notes")
    for body in ("One cat.", "Two fish.", "Three loaves."):
        post(api, chat, body)
    # One item per batch, and no time for a second.
    monkeypatch.setattr(search_index, "EMBED_BATCH", 1)

    assert search_index.embed_pending(store_id, budget=0) == 1
    assert embedded == ["One cat."]
    assert search_index.embed_pending(store_id) == 2
    assert embedded == ["One cat.", "Two fish.", "Three loaves."]


def test_a_long_item_is_embedded_by_its_start(api, store_id, embedded):
    """So a batch stays inside the embedding API's limit on tokens per request."""
    chat = make_chat(api, "Long")
    long = "cats " * search_index.EMBED_MAX_CHARS
    post(api, chat, long)
    search_index.embed_pending(store_id)
    assert embedded == [long[: search_index.EMBED_MAX_CHARS]]


def test_embedding_holds_no_store_session_while_the_embedding_api_answers(
    api, store_id, monkeypatch
):
    post(api, make_chat(api, "Evicted"), "Cats.")
    evicted = store_engine(store_id, "rw")

    async def embed_documents(batch):
        dispose_engines()  # as `MAX_ENGINES` newer engines would
        return [meaning(t) for t in batch]

    monkeypatch.setattr(search_index, "embed_documents", embed_documents)
    assert search_index.embed_pending(store_id) == 1
    assert evicted.pool.checkedin() == 0


def embedding_sweeps(store_id: uuid.UUID) -> list[WorkItem]:
    with SessionLocal() as s:
        return s.scalars(
            select(WorkItem).where(
                WorkItem.kind == WorkKind.embedding_sweep, WorkItem.data_store_id == store_id
            )
        ).all()


def test_unembedded_items_are_embedded_by_a_background_sweep_on_the_run_service(
    api, store_id, embedded
):
    post(api, make_chat(api, "Notes"), "One cat.")
    assert embedded == []

    # What each run service process does every `search_embed_interval_seconds`: an item per
    # ready store, in the background lane, queued once however many processes ask.
    asyncio.run(sweep.enqueue_embedding())
    asyncio.run(sweep.enqueue_embedding())
    [item] = embedding_sweeps(store_id)
    assert item.lane == WorkLane.background
    # Only this store's, here: the test database has many.
    with SessionLocal() as s:
        s.execute(delete(WorkItem).where(WorkItem.id != item.id))
        s.commit()

    asyncio.run(executor.drain_once())
    assert embedded == ["One cat."]
    assert embedding_sweeps(store_id) == []


def test_an_embedding_sweep_whose_attempts_run_out_is_dropped_for_the_next(db, user, caplog):
    org = org_of(db, user).id
    store = data_store_for(org)
    with SessionLocal() as s:
        queue.enqueue(s, [work.embedding_work(org, store)])
        s.commit()
    for _ in range(get_settings().queue_max_attempts):
        with SessionLocal() as s:
            [claimed] = queue.claim(s, 1, 1)
            s.execute(
                update(WorkItem)
                .where(WorkItem.id == claimed.id)
                .values(leased_until=func.now() - timedelta(seconds=1))
            )
            s.commit()

    asyncio.run(executor.drain_once())

    assert embedding_sweeps(store) == []
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_embedding_sweeps_never_take_the_slots_kept_for_chat(db, user, monkeypatch):
    org = org_of(db, user).id
    store = data_store_for(org)
    release = threading.Event()
    sweeping: list[int] = []

    async def slow_sweep(item):
        sweeping.append(item.id)
        await asyncio.to_thread(release.wait, 10)
        return work.Done()

    async def mention(item):
        return work.Done()

    monkeypatch.setitem(work.HANDLERS, WorkKind.embedding_sweep, slow_sweep)
    monkeypatch.setitem(work.HANDLERS, WorkKind.invocation_start, mention)
    sweeps = [
        queue.Work(org, store, WorkLane.background, WorkKind.embedding_sweep, {}, dedup_key=f"s{i}")
        for i in range(4)
    ]
    with SessionLocal() as s:
        queue.enqueue(s, sweeps)
        s.commit()

    async def scenario() -> tuple[int, int]:
        process = executor.Executor(concurrency=3, reserve=2)
        await process.claim()
        await asyncio.sleep(0.1)
        busy = len(sweeping)
        with SessionLocal() as s:
            queue.enqueue(
                s, [queue.Work(org, store, WorkLane.interactive, WorkKind.invocation_start, {})]
            )
            s.commit()
        claimed = await process.claim()
        release.set()
        await process.stop(grace=10)
        return busy, len(claimed)

    busy, chat = asyncio.run(scenario())
    # One background slot of three: one sweep at a time, and a mention still starts at once.
    assert (busy, chat) == (1, 1)


def test_ingest_indexes_a_documents_chunks_without_embedding_them(
    store_id, files_bucket, embedded, monkeypatch
):
    from conftest import add_document

    pieces = [
        {
            "text": "Zebrafish embryos are clear.",
            "contextualized": "Fish\nZebrafish embryos are clear.",
            "meta": {"headings": ["Fish"], "pages": [2]},
        },
    ]
    monkeypatch.setattr(ingest, "chunk_document", lambda filename, data: pieces)
    doc_id = add_document(store_id, b"# Fish", filename="fish.md")
    with store_session(store_id, "rw") as store:
        ingest.ingest_document(store, doc_id)
    assert embedded == []

    hits = asyncio.run(search.search(data_store_id=store_id, query="zebrafish", filters=Filters()))
    assert [(h.kind, h.filename, h.text, h.ref) for h in hits] == [
        (
            "document_chunk",
            "fish.md",
            "Zebrafish embryos are clear.",
            {"headings": ["Fish"], "pages": [2]},
        )
    ]
    # What's embedded is the passage with its headings.
    search_index.embed_pending(store_id)
    assert embedded == ["Fish\nZebrafish embryos are clear."]


def test_make_seed_indexes_its_chat_through_the_writers(db, embedded):
    from aios import seed

    seed.build(db)
    made = db.scalar(select(User).where(User.email == seed.EMAIL))
    try:
        hits = asyncio.run(search.search(data_store_id=store_of(made), query="weekly notes"))
        assert [h.kind for h in hits] == ["chat_message"]
        assert hits[0].chat_name == "General"
    finally:
        delete_user(db, made)


# --- The tools -----------------------------------------------------------------------------------


def searches_with(tool: str, **args):
    """A model that calls the tool once with `args`, then replies with what came back."""
    seen = {}

    async def script(msgs, info):
        if returns := last_tool_results(msgs):
            seen["results"] = tool_output(returns[0])
            yield f"found {len(seen['results'])}"
        else:
            seen["description"] = info.tools[tool].description
            yield ToolCall(tool, args)

    return model(script), seen


def with_tool(api, name: str, key: str, config: dict | None = None) -> dict:
    agent = make_agent(api, name)
    tools = [{"key": key, "config": config or {}}]
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=tools)).json()


def ask(api, agent: dict, tool: str, run: dict | None = None, **args) -> dict:
    fake, seen = searches_with(tool, **args)
    run = run or new_run(api, agent)
    with use_model(fake):
        take_turn(api, agent["id"], run["id"], "look it up")
    return seen


def test_the_tool_library_offers_search_and_keeps_the_old_search_keys(api):
    tools = {t["key"]: t for t in api.get("/api/tools").json()}
    assert tools["search"]["name"] == "Search"
    assert tools["search"]["default_config"] == {
        "max_results": 8,
        "kinds": ["document_chunk", "chat_message", "run_message", "brief"],
    }
    max_results = tools["search"]["config_schema"]["properties"]["max_results"]
    assert (max_results["minimum"], max_results["maximum"]) == (1, 50)
    assert (tools["history_search"]["name"], tools["history_search"]["default_config"]) == (
        "Run history search",
        {"max_results": 8},
    )
    assert tools["hybrid_search"]["default_config"] == {"max_results": 8}


@pytest.fixture
def two_chats(api, user, embedded) -> dict:
    """An agent mentioned in one chat, whose handoff briefed it; and a message in another."""
    helper = make_agent(api, "Helper")
    briefed = make_chat(api, "Planning")
    add(api, briefed, "agent", helper)

    async def handoff(msgs, info):
        yield ToolCall("Handoff", {"ack": "On it.", "brief": "The kitten adoption is Friday."})

    with use_handoff(model(handoff)), use_model(replies("Noted.")):
        response = api.post(
            f"/api/chats/{briefed['id']}/messages",
            json={"body": "Remember Friday", "mentions": [{"kind": "agent", "id": helper["id"]}]},
        )
    assert response.status_code == 201, response.text
    other = make_chat(api, "Garden")
    post(api, other, "A stray kitten sleeps in the shed.")
    search_index.embed_pending(store_of(user))
    return {"helper": helper, "briefed": briefed, "other": other}


def test_an_agent_finds_any_chats_messages_and_briefs_whichever_chat_started_it(api, two_chats):
    agent = with_tool(api, "Finder", "search")
    elsewhere = make_chat(api, "Elsewhere")
    add(api, elsewhere, "agent", agent)
    fake, seen = searches_with("search", query="kitten", kinds=["chat_message", "brief"])
    with use_model(fake):
        response = api.post(
            f"/api/chats/{elsewhere['id']}/messages",
            json={"body": "Find kittens", "mentions": [{"kind": "agent", "id": agent["id"]}]},
        )
    assert response.status_code == 201
    assert "Search everything in the organisation" in seen["description"]
    got = {(r["kind"], r["chat"], r["text"]) for r in seen["results"]}
    assert {r["kind"] for r in seen["results"]} == {"chat_message", "brief"}
    assert got >= {
        ("chat_message", "Garden", "A stray kitten sleeps in the shed."),
        ("brief", "Planning", "The kitten adoption is Friday."),
    }


def test_a_run_by_hand_or_by_schedule_finds_them_too_and_filters_by_chat_and_agent(
    api, db, two_chats
):
    from test_organisation_runs import fire

    agent = with_tool(api, "Finder", "search")
    seen = ask(api, agent, "search", query="kitten", chat_id=two_chats["other"]["id"])
    assert [(r["kind"], r["chat_id"]) for r in seen["results"]] == [
        ("chat_message", two_chats["other"]["id"])
    ]
    # By or for the agent: its brief, its run's messages, and what it posted.
    seen = ask(api, agent, "search", query="kitten", agent_id=two_chats["helper"]["id"])
    assert {r["agent"] for r in seen["results"]} == {"Helper"}
    assert "brief" in {r["kind"] for r in seen["results"]}

    fake, seen = searches_with("search", query="kitten adoption")
    with use_model(fake):
        fired = fire(api, db, "agent", agent)
    assert fired["last_status"] == "succeeded", fired
    assert "The kitten adoption is Friday." in {r["text"] for r in seen["results"]}


def test_the_search_tool_filters_by_time_and_caps_its_results(api, two_chats):
    agent = with_tool(api, "Finder", "search", {"max_results": 1})
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    assert ask(api, agent, "search", query="kitten", since=future)["results"] == []
    assert ask(api, agent, "search", query="kitten", until=past)["results"] == []
    # Asking for more than its settings allow gets what they allow.
    assert len(ask(api, agent, "search", query="kitten", limit=50)["results"]) == 1


def test_the_search_tools_settings_limit_the_kinds_it_finds(api, two_chats):
    agent = with_tool(api, "Finder", "search", {"kinds": ["brief"]})
    assert {r["kind"] for r in ask(api, agent, "search", query="kitten")["results"]} == {"brief"}
    refused = ask(api, agent, "search", query="kitten", kinds=["chat_message"])["results"]
    assert refused == [{"error": "This agent can't search those kinds of item."}]


@pytest.fixture
def past_runs(api, store_id, embedded) -> dict:
    """Two earlier runs, embedded: one about baking, one about cats."""
    baker = make_agent(api, "Baker")
    baking = new_run(api, baker)
    chat_turn(api, baker, baking, "How do I keep my sourdough going?", "Feed the starter daily.")
    cats = new_run(api, baker)
    chat_turn(api, baker, cats, "Why do felines knead?", "It comforts them, Kneading-wise.")
    search_index.embed_pending(store_id)
    return {"baker": baker, "baking": baking, "cats": cats}


HISTORY_TOOL = {"max_results": 3}


def test_the_history_search_key_still_runs_with_its_old_result_shape(api, past_runs, in_store):
    from aios.datastores.models import AgentRun

    agent = with_tool(api, "Rememberer", "history_search", HISTORY_TOOL)
    baking = past_runs["baking"]
    started = in_store.get(AgentRun, baking["id"]).created_at

    # By meaning: "bread" shares no word with the run, only its meaning.
    seen = ask(api, agent, "search_history", query="bread")
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
    seen = ask(api, agent, "search_history", query="starter")
    assert seen["results"][0]["text"] == "Feed the starter daily."
    assert seen["results"][0]["run_id"] == baking["id"]


def test_searching_history_leaves_out_the_run_searching_and_finds_only_runs(
    api, store_id, past_runs, embedded
):
    agent = with_tool(api, "Rememberer", "history_search", {"max_results": 10})
    run = new_run(api, agent)
    chat_turn(api, agent, run, "Kittens and sourdough", "Both need feeding.")
    post(api, make_chat(api, "Bakery"), "Sourdough and kittens, again.")
    search_index.embed_pending(store_id)

    seen = ask(api, agent, "search_history", run=run, query="kittens sourdough")

    assert {r["run_id"] for r in seen["results"]} == {
        past_runs["baking"]["id"],
        past_runs["cats"]["id"],
    }


def test_the_document_search_key_still_runs_with_its_old_result_shape(api, store_id, embedded):
    add_chunks(store_id, "fish.md", [("Zebrafish fins.", unit(1))], headings=["Fish"])
    post(api, make_chat(api, "Tank"), "Zebrafish everywhere.")
    agent = with_tool(api, "Librarian", "hybrid_search")

    seen = ask(api, agent, "search_documents", query="zebrafish")

    assert seen["results"] == [
        {"source": "fish.md", "headings": ["Fish"], "text": "Zebrafish fins."}
    ]
