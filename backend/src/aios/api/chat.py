import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from aios import tracing, ui_stream
from aios.agent import ChatDeps, documents_agent
from aios.auth import CurrentUser
from aios.datastores import data_store_for

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.post("/chat")
async def chat(body: dict, user: CurrentUser):
    # Accepts the AI SDK `useChat` request body and streams back the UI message stream protocol.
    # The conversation lives in the client, which sends all of it each turn.
    messages = ui_stream.to_langchain(ui_stream.parse_request(body).messages)
    data_store_id = await run_in_threadpool(data_store_for, user.id)

    async def stream() -> AsyncIterator[ui_stream.Chunk]:
        ui = ui_stream.UIStream()
        for chunk in ui.start():
            yield chunk
        try:
            agent = documents_agent()
            async for mode, data in agent.astream(
                {"messages": messages},
                {
                    "callbacks": tracing.callbacks(),
                    "run_name": "documents-chat",
                    "metadata": tracing.trace_metadata(
                        session_id=None, user_id=user.id, tags=["documents-chat"]
                    ),
                },
                context=ChatDeps(user_id=user.id, data_store_id=data_store_id),
                stream_mode=["messages", "updates"],
            ):
                for chunk in ui.feed(mode, data):
                    yield chunk
        except Exception as exc:
            log.exception("documents chat failed")
            for chunk in ui.error(exc):
                yield chunk
        for chunk in ui.finish():
            yield chunk

    return ui_stream.streaming_response(stream())
