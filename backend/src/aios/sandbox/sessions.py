"""One sandbox per agent run: started on first use, reused by later calls, replaced if it's gone."""

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from anyio import to_thread
from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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


def _save(
    run_id: int, handle: SandboxHandle, *, replaced: bool, previous: SandboxHandle | None
) -> bool:
    """Records `handle` as the run's sandbox if the run's row still holds `previous` (or, with
    None, has no row yet).

    False if another request got there first, in this process or another replica: its sandbox
    is the run's now. Raises `IntegrityError` if the run has been deleted.
    """
    values = {
        "ref": handle.ref,
        "endpoint": handle.endpoint,
        "secret": handle.secret,
        "replaced": replaced,
    }
    with SessionLocal() as db:
        if previous is None:
            statement = (
                insert(SandboxSession)
                .values(agent_run_id=run_id, **values)
                .on_conflict_do_nothing(index_elements=[SandboxSession.agent_run_id])
            )
        else:
            statement = (
                update(SandboxSession)
                .where(
                    SandboxSession.agent_run_id == run_id,
                    SandboxSession.ref == previous.ref,
                )
                .values(**values, created_at=func.now(), last_used_at=func.now())
            )
        saved = db.scalar(statement.returning(SandboxSession.id)) is not None
        db.commit()
    return saved


def _touch(run_id: int) -> None:
    with SessionLocal() as db:
        db.execute(
            update(SandboxSession)
            .where(SandboxSession.agent_run_id == run_id)
            .values(last_used_at=func.now(), replaced=False)
        )
        db.commit()


# Work that finishes even if the request that started it is cancelled (see `_shielded`), kept so
# it isn't garbage-collected before it finishes.
_background: set[asyncio.Task] = set()


def _forget(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled():
        task.exception()  # Retrieved: whoever awaited it has had it, or was cancelled.


async def _shielded[T](work: Coroutine[Any, Any, T]) -> T:
    """Runs `work` to the end even if the caller is cancelled meanwhile."""
    task = asyncio.ensure_future(work)
    _background.add(task)
    task.add_done_callback(_forget)
    return await asyncio.shield(task)


async def _stop_quietly(backend: SandboxBackend, handle: SandboxHandle) -> None:
    """Stops a sandbox, logging a failure rather than raising it."""
    try:
        await backend.stop(handle)
    except Exception:
        log.warning("couldn't stop sandbox %s", handle.ref, exc_info=True)


def run_sandboxes(db: Session, run_ids: Select | list[int]) -> list[SandboxHandle]:
    """The sandboxes of these runs, e.g. to stop them once the runs are deleted."""
    rows = db.scalars(select(SandboxSession).where(SandboxSession.agent_run_id.in_(run_ids)))
    return [SandboxHandle(row.ref, row.endpoint, row.secret) for row in rows]


def worth_prewarming(db: Session, run_id: int, *, first_turn: bool) -> bool:
    """Whether to start the run's sandbox when a turn starts, before the model asks for it.

    On the run's first turn, to hide the start-up time from its first call. Later, only while
    the run is using its sandbox: if it has never used one, or not for long enough that it has
    stopped as idle, a turn that might not need one doesn't start one.
    """
    last_used, now = db.execute(
        select(SandboxSession.last_used_at, func.now()).where(SandboxSession.agent_run_id == run_id)
    ).one_or_none() or (None, None)
    if last_used is None:
        return first_turn
    return now - last_used < timedelta(seconds=get_settings().sandbox_idle_seconds)


async def stop_sandboxes(handles: list[SandboxHandle]) -> None:
    """Stops sandboxes that no run uses any more, rather than leaving them to time out."""
    try:
        backend = get_backend()
    except SandboxError:
        log.exception("couldn't stop %d sandboxes", len(handles))
        return
    if backend is not None:
        for handle in handles:
            await _stop_quietly(backend, handle)


class SessionManager:
    def __init__(self) -> None:
        # Serialises starting and calling each run's sandbox within this process. Across
        # processes (two replicas serving turns of the same run at once), a new sandbox is only
        # recorded if the run's row hasn't changed since it was read, and one that loses is
        # stopped (see `_start`).
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
        self,
        backend: SandboxBackend,
        run_id: int,
        allowed_domains: list[str],
        *,
        previous: SandboxHandle | None,
        replaced: bool,
    ) -> tuple[SandboxHandle, bool]:
        """Starts a sandbox for the run, in place of `previous` (its last one, which has gone).

        Returns the run's sandbox and whether it replaced one. That's another request's sandbox
        if it started one at the same time and recorded it first.
        """
        settings = get_settings()
        session_key = f"agent-run-{run_id}"
        proxy_url = ""
        secret = settings.sandbox_proxy_secret.get_secret_value()
        # A backend whose sandboxes already have unrestricted network (the dev one) needs no
        # proxy: the allowed domains are reachable anyway.
        needs_proxy = bool(allowed_domains) and backend.restricts_network
        if needs_proxy and not (settings.sandbox_proxy_address and secret):
            # Not started without a proxy: the model is told these domains are reachable.
            log.error(
                "run %s's sandbox needs network access, but SANDBOX_PROXY_ADDRESS or"
                " SANDBOX_PROXY_SECRET isn't set",
                run_id,
            )
            raise SandboxNotConfigured(
                "Network access for the sandbox isn't set up on this server, so the allowed"
                " domains can't be reached. Ask the server's administrator to set it up."
            )
        if needs_proxy:
            token = proxy_token.mint(
                secret,
                session=session_key,
                domains=allowed_domains,
                # The sandbox's maximum lifetime counts from when it boots, which can be up to
                # the start timeout from now, so the token must outlast both.
                ttl=settings.sandbox_start_timeout + settings.sandbox_max_session_seconds,
            )
            proxy_url = f"http://sandbox:{token}@{settings.sandbox_proxy_address}"
        spec = SessionSpec(
            session_key=session_key,
            proxy_url=proxy_url,
            idle_seconds=settings.sandbox_idle_seconds,
            max_seconds=settings.sandbox_max_session_seconds,
        )
        handle = await backend.start(spec)

        async def record() -> bool:
            try:
                saved = await to_thread.run_sync(
                    lambda: _save(run_id, handle, replaced=replaced, previous=previous)
                )
            except IntegrityError:
                saved = False  # The run was deleted while its sandbox started.
            except BaseException:
                # E.g. the database is unreachable: nothing would find this sandbox.
                await _stop_quietly(backend, handle)
                raise
            if not saved:
                await _stop_quietly(backend, handle)
            return saved

        # Shielded: if this request is cancelled now, the new sandbox is still either recorded
        # for the run's next call or stopped, never left running with nothing pointing at it.
        if not await _shielded(record()):
            current, current_replaced = await to_thread.run_sync(_load, run_id)
            if current is None:
                raise SandboxError("This run no longer exists.")
            log.info("another request started %s's sandbox first: %s", session_key, current.ref)
            return current, current_replaced
        if previous is not None:
            # Usually stopped already, but it may only have been unreachable for a while (e.g.
            # too busy to answer a health check). It's no longer the run's, so make sure.
            await _shielded(_stop_quietly(backend, previous))
        log.info("started sandbox %s for %s", handle.ref, session_key)
        return handle, replaced

    async def _ready(
        self, backend: SandboxBackend, run_id: int, allowed_domains: list[str]
    ) -> tuple[SandboxHandle, bool]:
        """The run's running sandbox, started if it has none or its last one has gone."""
        handle, replaced = await to_thread.run_sync(_load, run_id)
        if handle is not None:
            if await backend.alive(handle):
                return handle, replaced
            log.info("sandbox %s for run %s is gone; starting another", handle.ref, run_id)
            replaced = True
        return await self._start(
            backend, run_id, allowed_domains, previous=handle, replaced=replaced
        )

    async def ensure(self, *, run_id: int, allowed_domains: list[str]) -> None:
        """Makes sure the run has a running sandbox, so it's ready for the next call."""
        backend = self._backend()
        async with self._lock(run_id):
            await self._ready(backend, run_id, allowed_domains)

    async def run(
        self, *, run_id: int, code: str, timeout: int, allowed_domains: list[str]
    ) -> Outcome:
        backend = self._backend()
        async with self._lock(run_id):
            # Checked before the call, so a sandbox that has gone (e.g. stopped when idle) is
            # replaced without the code having run anywhere.
            handle, replaced = await self._ready(backend, run_id, allowed_domains)
            try:
                result = await backend.execute(handle, code, timeout)
            except SandboxGone:
                # It stopped during the call (e.g. the code ran out of memory, or the sandbox
                # reached its maximum lifetime), so the code may have partly run: it isn't run
                # again, which could repeat its side effects. The next call starts a new one.
                log.info("sandbox %s for run %s stopped during a call", handle.ref, run_id)
                raise SandboxGone(
                    "The sandbox stopped while running this code (for example, it ran out of"
                    " memory or reached its time limit), so the code may have partly run. The"
                    " next call starts a new sandbox, without the files in /work."
                ) from None
            await to_thread.run_sync(_touch, run_id)
            return Outcome(result, replaced)


manager = SessionManager()
