from fastapi import APIRouter, Request
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from aios.agent import ChatDeps, agent
from aios.auth import CurrentUser

router = APIRouter(prefix="/api")


@router.post("/chat")
async def chat(request: Request, user: CurrentUser):
    # Accepts the AI SDK `useChat` request body and streams back the UI message stream protocol.
    return await VercelAIAdapter.dispatch_request(
        request,
        agent=agent,
        deps=ChatDeps(user_id=user.id),
        sdk_version=7,
        # The UI doesn't send attachments; don't let clients make the server fetch URLs.
        allowed_file_url_schemes=frozenset(),
    )
