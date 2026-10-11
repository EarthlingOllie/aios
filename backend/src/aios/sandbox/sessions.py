"""One sandbox per agent run: started on first use, reused by later calls, replaced if it's gone."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from anyio import to_thread
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import SandboxSession
from aios.sandbox import proxy_token
from aios.sandbox.backends import get_backend
from aios.sandbox.base import (
    ExecResult,
    SandboxBackend,
    SandboxError,
    SandboxGone,
    SandboxHandle,
    SessionSpec,
)

log = logging.getLogger(__name__)


class SandboxNotConfigured(SandboxError):
    pass


@dataclass(frozen=True)
class Outcome:
    result: ExecResult
    # The run's previous sandbox had gone (e.g. stopped when idle), so this ran in a fresh one:
    # files earlier calls left in /work are gone.
    restarted: bool


def _load(run_id: int) -> tuple[SandboxHandle | None, bool]:
    """The run's sandbox, and whether it replaced one that had gone (see `SandboxSession`)."""
    with SessionLocal() as db:
        row = db.scalar(select(SandboxSession).where(SandboxSession.agent_run_id == run_id))
        if row is None:
            return None, False
        return SandboxHandle(row.ref, row.endpoint, row.secret), row.replaced


def _save(run_id: int, handle: SandboxHandle, *, replaced: bool) -> None:
    values = {
        "ref": handle.ref,
        "endpoint": handle.endpoint,
        "secret": handle.secret,
        "replaced": replaced,
    }
    with SessionLocal() as db:
        db.execute(
            insert(SandboxSession)
            .values(agent_run_id=run_id, **values)
            .on_conflict_do_update(
                index_elements=[SandboxSession.agent_run_id],
                set_={**values, "created_at": func.now(), "last_used_at": func.now()},
            )
        )
        db.commit()


def _touch(run_id: int) -> None:
    with SessionLocal() as db:
        db.execute(
            update(SandboxSession)
            .where(SandboxSession.agent_run_id == run_id)
            .values(last_used_at=func.now(), replaced=False)
        )
        db.commit()


class SessionManager:
    def __init__(self) -> None:
        # Serialises starting and calling each run's sandbox within this process. A run's turns
        # are served one at a time, by one request, so its calls don't race across replicas.
        self._locks: dict[int, asyncio.Lock] = {}
        self._waiting: dict[int, int] = {}

    @asynccontextmanager
    async def _lock(self, run_id: int) -> AsyncIterator[None]:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        self._waiting[run_id] = self._waiting.get(run_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._waiting[run_id] -= 1
            if not self._waiting[run_id]:
                del self._waiting[run_id], self._locks[run_id]

    @staticmethod
    def _backend() -> SandboxBackend:
        backend = get_backend()
        if backend is None:
            raise SandboxNotConfigured("Running Python isn't set up on this server.")
        return backend

    async def _start(
        self, backend: SandboxBackend, run_id: int, allowed_domains: list[str], *, replaced: bool
    ) -> SandboxHandle:
        settings = get_settings()
        session_key = f"agent-run-{run_id}"
        proxy_url = ""
        secret = settings.sandbox_proxy_secret.get_secret_value()
        if allowed_domains and settings.sandbox_proxy_address and secret:
            token = proxy_token.mint(
                secret,
                session=session_key,
                domains=allowed_domains,
                ttl=settings.sandbox_max_session_seconds,
            )
            proxy_url = f"http://sandbox:{token}@{settings.sandbox_proxy_address}"
        spec = SessionSpec(
            session_key=session_key,
            proxy_url=proxy_url,
            idle_seconds=settings.sandbox_idle_seconds,
            max_seconds=settings.sandbox_max_session_seconds,
        )
        handle = await backend.start(spec)
        try:
            await to_thread.run_sync(lambda: _save(run_id, handle, replaced=replaced))
        except IntegrityError:
            # The run was deleted while its sandbox started.
            await backend.stop(handle)
            raise SandboxError("This run no longer exists.") from None
        log.info("started sandbox %s for %s", handle.ref, session_key)
        return handle

    async def ensure(self, *, run_id: int, allowed_domains: list[str]) -> None:
        """Makes sure the run has a running sandbox, so it's ready for the next call."""
        backend = self._backend()
        async with self._lock(run_id):
            handle, replaced = await to_thread.run_sync(_load, run_id)
            if handle is not None:
                if await backend.alive(handle):
                    return
                log.info("sandbox %s for run %s is gone; starting another", handle.ref, run_id)
                replaced = True
            await self._start(backend, run_id, allowed_domains, replaced=replaced)

    async def run(
        self, *, run_id: int, code: str, timeout: int, allowed_domains: list[str]
    ) -> Outcome:
        backend = self._backend()
        async with self._lock(run_id):
            handle, replaced = await to_thread.run_sync(_load, run_id)
            if handle is None:
                handle = await self._start(backend, run_id, allowed_domains, replaced=False)
            try:
                result = await backend.execute(handle, code, timeout)
            except SandboxGone:
                log.info("sandbox %s for run %s is gone; starting another", handle.ref, run_id)
                handle = await self._start(backend, run_id, allowed_domains, replaced=True)
                replaced = True
                result = await backend.execute(handle, code, timeout)
            await to_thread.run_sync(_touch, run_id)
            return Outcome(result, replaced)


manager = SessionManager()
