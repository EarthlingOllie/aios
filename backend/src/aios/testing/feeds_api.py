"""The test provider's testing routes: what happens upstream, for the full-stack test. A story
puts a message in a feed (a channel), deletes an item from one, reads one (to see what an
agent's tool posted), and puts or takes away a page a URL source reads
(`aios.source_providers.test`).

`create_app` mounts this router only with `SOURCES_TEST_FEEDS_SHARED` on (the full-stack test's
stack), so otherwise its routes don't exist; the feeds are then kept in Redis, so the run
service's syncs and tools see what these change. Any signed-in person may use them: the feeds
are the test provider's, not an organisation's.
"""

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from aios.auth import current_user, same_origin
from aios.source_providers import test as provider

router = APIRouter(
    prefix="/api/testing",
    dependencies=[Depends(current_user), Depends(same_origin)],
    include_in_schema=False,
)


class MessageIn(BaseModel):
    text: str
    title: str | None = None


class PageIn(BaseModel):
    text: str
    content_type: str = "text/markdown"


class FeedItemOut(BaseModel):
    id: str
    title: str
    text: str | None


class FeedOut(BaseModel):
    name: str
    items: list[FeedItemOut]


@router.get("/feeds/{name}", response_model=FeedOut)
def read_feed(name: str):
    """The feed's items as they are upstream (empty if there's no such feed)."""
    f = provider.load(name)
    entries = [] if f is None else list(f.items.values())
    return FeedOut(
        name=name,
        items=[
            FeedItemOut(id=e.item.external_id, title=e.item.title, text=e.content.text)
            for e in entries
        ],
    )


@router.put("/feeds/{name}/messages/{external_id}", status_code=status.HTTP_204_NO_CONTENT)
def put_message(name: str, external_id: str, body: MessageIn) -> Response:
    """Put a message item in the feed (made if there's none), new or changed."""
    provider.feed(name).message(external_id, body.text, title=body.title)
    provider.save(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/feeds/{name}/items/{external_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(name: str, external_id: str) -> Response:
    """Delete the item upstream: the feed's changes say so."""
    provider.feed(name).delete(external_id)
    provider.save(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/pages/{path:path}", status_code=status.HTTP_204_NO_CONTENT)
def put_page(path: str, body: PageIn) -> Response:
    """Put a page at `https://test.invalid/pages/<path>`, new or changed."""
    provider.page(path, body.text, body.content_type)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/pages/{path:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_page(path: str) -> Response:
    """Take the page away: "not found" from now."""
    provider.remove_page(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
