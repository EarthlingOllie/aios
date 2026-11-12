"""The Search page's endpoint: the organisation's search index (`aios.search`), every kind of
item in it, from the whole organisation (everyone in it can find everything), but agents'
memories, which only their own agent recalls (`aios.memory`): they're on its Memory tab."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from aios import usage
from aios.auth import CurrentDataStore, CurrentUser
from aios.models import UsagePurpose
from aios.permissions import Action, allow
from aios.search import Filters
from aios.search import search as search_index
from aios.tools.search import Searchable
from aios.usage import Charge

router = APIRouter(prefix="/api")


class SearchHitOut(BaseModel):
    """An item found, labelled with its kind, with what links it to its source: a document
    passage's document, a chat message's chat (and message), a run message's run and agent, a
    brief's chat and agent, a synced item's source and place there; and how each search branch
    ranked it (the ranking explanation)."""

    id: int
    kind: Searchable
    text: str
    created_at: datetime
    # A passage's headings and pages; a run message's role.
    ref: dict
    document_id: int | None
    filename: str | None
    chat_id: int | None
    chat_name: str | None
    message_id: int | None
    agent_id: int | None
    agent_name: str | None
    agent_run_id: int | None
    run_title: str | None
    # A synced item's (a source item's, or a synced document passage's): its item, title, place
    # at its source and its source's name.
    synced_item_id: int | None = None
    item_title: str | None = None
    item_url: str | None = None
    source_name: str | None = None
    # Reciprocal Rank Fusion score: higher is better; and the item's rank in each branch, or
    # null if that branch didn't find it.
    score: float
    semantic_rank: int | None
    lexical_rank: int | None


@router.get("/search", response_model=list[SearchHitOut], dependencies=[allow(Action.read)])
async def search(
    store_id: CurrentDataStore,
    user: CurrentUser,
    # At least one non-whitespace character.
    q: Annotated[str, Query(min_length=1, pattern=r"\S")],
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
    # Any of these kinds; every kind if none is given.
    kind: Annotated[list[Searchable] | None, Query()] = None,
):
    # The query's embedding is charged to the person searching (`aios.usage`).
    with usage.charging(Charge(store_id, UsagePurpose.search_query, person_id=user.id)):
        return await search_index(
            data_store_id=store_id, query=q, limit=limit, filters=Filters(kinds=kind or ())
        )
