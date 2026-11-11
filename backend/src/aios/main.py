import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from aios import auth, dev_auth, scheduler, tracing
from aios.api import (
    agents,
    chats,
    data,
    documents,
    dropped,
    me,
    notifications,
    organisation,
    schema_changes,
    skills,
    sources,
    tools,
    triggers,
    workflows,
)
from aios.api import approval_rules as approval_rules_api
from aios.api import audit as audit_api
from aios.api import flagged as flagged_api
from aios.api import search as search_api
from aios.api.scoping import mount_organisation_prefix
from aios.config import get_settings
from aios.datastores import DataStoreBehind, DataStoreBusy, DataStoreNotReady
from aios.db import engine
from aios.models import DataStoreStatus

log = logging.getLogger(__name__)


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
    if settings.scripted_models_enabled and settings.cookie_secure:
        raise RuntimeError(
            "SCRIPTED_MODELS_ENABLED is for the full-stack test only, and can't be on with "
            "COOKIE_SECURE (a production-like configuration)"
        )
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
    app.include_router(me.router)
    app.include_router(documents.router)
    app.include_router(data.router)
    app.include_router(skills.router)
    app.include_router(agents.router)
    app.include_router(tools.router)
    app.include_router(workflows.router)
    app.include_router(triggers.router)
    app.include_router(schema_changes.router)
    app.include_router(audit_api.router)
    app.include_router(flagged_api.router)
    app.include_router(approval_rules_api.router)
    app.include_router(dropped.router)
    app.include_router(sources.router)
    app.include_router(notifications.router)
    app.include_router(search_api.router)
    app.include_router(organisation.router)
    app.include_router(chats.router)
    if settings.dev_login_enabled:
        log.warning("DEV_LOGIN_ENABLED is on: anyone can sign in as any user, with no password")
        app.include_router(dev_auth.router)
    # After every router: each organisation-scoped route is mounted under the prefix too.
    mount_organisation_prefix(app)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own handler, except that a non-finite number in an error's echoed input
        # (JSON's 1e309 parses to infinity) is sent as a string: JSON has no way to write it,
        # so it would turn the 422 into a 500. The body is left out for the same reason.
        errors = jsonable_encoder(
            exc.errors(), custom_encoder={float: lambda f: f if math.isfinite(f) else str(f)}
        )
        return await request_validation_exception_handler(request, RequestValidationError(errors))

    @app.exception_handler(DataStoreNotReady)
    async def data_store_not_ready(request: Request, exc: DataStoreNotReady) -> JSONResponse:
        # Uploads, document lists and searches, until the organisation's store is provisioned
        # (or, made by the release before during a deploy, brought to the store tree's head).
        if isinstance(exc, DataStoreBehind):
            detail = "The organisation's data store is being upgraded. Try again in a moment."
        elif exc.status == DataStoreStatus.failed:
            detail = (
                "The organisation's data store couldn't be set up. We'll try again; please check"
                " back later."
            )
        else:
            detail = "The organisation's data store is being set up. Try again in a moment."
        return JSONResponse(
            {"detail": detail, "data_store_status": exc.status.value},
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(DataStoreBusy)
    async def data_store_busy(request: Request, exc: DataStoreBusy) -> JSONResponse:
        # The store's role (or its instance) is at its connection limit: a moment's wait frees one.
        return JSONResponse(
            {"detail": "The organisation's data store is busy. Try again in a moment."},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
        )

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
