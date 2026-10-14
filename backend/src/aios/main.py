import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from aios import auth, scheduler, tracing
from aios.api import agents, chat, documents, skills, tools, triggers, workflows
from aios.config import get_settings
from aios.db import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    loop = None
    if settings.scheduler_enabled:
        loop = asyncio.create_task(scheduler.run_forever(settings.scheduler_interval_seconds))
    yield
    if loop is not None:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    tracing.flush()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="aios", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie=settings.session_cookie,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(chat.router)
    app.include_router(skills.router)
    app.include_router(agents.router)
    app.include_router(tools.router)
    app.include_router(workflows.router)
    app.include_router(triggers.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
