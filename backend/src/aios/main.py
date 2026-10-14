import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from aios import auth, tracing
from aios.api import agents, chat, documents, skills, tools, workflows
from aios.config import get_settings
from aios.db import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
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

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own handler, except that a non-finite number in an error's echoed input
        # (JSON's 1e309 parses to infinity) is sent as a string: JSON has no way to write it,
        # so it would turn the 422 into a 500.
        detail = jsonable_encoder(
            exc.errors(), custom_encoder={float: lambda f: f if math.isfinite(f) else str(f)}
        )
        return JSONResponse({"detail": detail}, status.HTTP_422_UNPROCESSABLE_CONTENT)

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
