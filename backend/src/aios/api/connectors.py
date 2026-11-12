"""The organisation's connectors (`aios.connectors`): every member sees them, their scope and
health, and the providers this deployment offers; only admins connect, reconnect, choose what
syncs and remove them. Credentials are never returned.

The connect flow's callback is the one route outside the organisation's path, because providers
send the browser back to a fixed address; the signed state names the organisation.
"""

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aios import audit, connectors, organisations, source_providers
from aios.api.sources import SourceOut, sources_out
from aios.auth import CurrentDataStore, CurrentOrganisation, CurrentUser
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import (
    Connector,
    ConnectorStatus,
    Source,
    SourceKind,
    SourceStatus,
    SyncKind,
)
from aios.datastores.resolve import data_store_for
from aios.db import DB, DbId, SessionLocal
from aios.permissions import Action, allow, can
from aios.run_service.work import enqueue_sync
from aios.source_providers import NeedsReconnecting, ProviderUnavailable, SourceView
from aios.tools import connector as tool_kind

router = APIRouter(prefix="/api")

CALLBACK = "/api/connectors/callback"
REMOVING = "This connector is being removed."
NEEDS_RECONNECTING = "This connector needs reconnecting first."
NOT_ADMIN = "Only an admin can connect a system."


class ProviderOut(BaseModel):
    provider: str
    title: str
    # Why it can't be connected here; null if it can.
    unavailable: str | None
    # Its connector in this organisation, if it has one.
    connector_id: int | None


class ScopeItemOut(BaseModel):
    id: str
    name: str
    # Private at its source: picking it needs the acknowledgement.
    restricted: bool


class ConnectorOut(BaseModel):
    id: int
    provider: str
    name: str
    # active, needs_reauth or removing.
    status: ConnectorStatus
    # Why its provider can't be used here now (switched off, say); null if it can. While it
    # can't, the connector doesn't sync, and its content stays searchable.
    unavailable: str | None
    scope: list[ScopeItemOut]
    # Its source (made when its scope is first saved), with its health and last sync.
    source_id: int | None
    source: SourceOut | None
    created_at: datetime


class ToolOut(BaseModel):
    """One of the connector's tools, as its provider offers it, for the agent editor."""

    name: str
    description: str
    input_schema: dict
    # Marked read-only by the provider: calling it changes nothing (and isn't audited).
    read_only: bool


class StartIn(BaseModel):
    provider: str = Field(min_length=1, max_length=64)


class StartOut(BaseModel):
    # Where to send the browser: the provider's authorisation page.
    redirect_url: str


class ScopeIn(BaseModel):
    # The ids of the scope options picked.
    items: list[str] = Field(max_length=1000)
    # Accepting that everyone in the organisation will be able to search what's restricted.
    acknowledge_restricted: bool = False


def _title(provider: str) -> str:
    found = source_providers.provider(provider)
    return found.title if found else provider


def _outs(store: Session, rows: list[Connector]) -> list[ConnectorOut]:
    sources = {s.id: s for s in sources_out(store)}
    by_connector = dict(
        store.execute(
            select(Source.connector_id, Source.id).where(Source.connector_id.is_not(None))
        ).all()
    )
    outs = []
    for c in rows:
        source_id = by_connector.get(c.id)
        outs.append(
            ConnectorOut(
                id=c.id,
                provider=c.provider,
                name=c.name,
                status=c.status,
                unavailable=source_providers.why_unavailable(c.provider),
                scope=[ScopeItemOut(**i) for i in c.scope or []],
                source_id=source_id,
                source=sources.get(source_id),
                created_at=c.created_at,
            )
        )
    return outs


def _one(store: Session, connector_id: int) -> ConnectorOut:
    connector = store.get(Connector, connector_id)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _outs(store, [connector])[0]


def _found(store: Session, connector_id: int) -> Connector:
    """The connector, unlocked (for a read-only session): 404 if it isn't the organisation's,
    409 while it's being removed."""
    return _locked(store, connector_id, lock=False)


def _locked(store: Session, connector_id: int, *, lock: bool = True) -> Connector:
    query = select(Connector).where(Connector.id == connector_id)
    connector = store.scalar(query.with_for_update() if lock else query)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if connector.status == ConnectorStatus.removing:
        raise HTTPException(status.HTTP_409_CONFLICT, REMOVING)
    return connector


def _usable(provider: str) -> source_providers.Provider:
    """The provider, if a connector can be made with it here (else 409 with why not)."""
    why = source_providers.why_unavailable(provider)
    found = source_providers.provider(provider)
    if why is None and (found is None or found.authorise_url is None):
        why = f"{_title(provider)} can't be connected yet."
    if why is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, why)
    return found


def _already(provider: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"This organisation already has a {_title(provider)} connector: remove it to connect "
        "another.",
    )


@router.get(
    "/connector-providers", response_model=list[ProviderOut], dependencies=[allow(Action.read)]
)
def list_providers(store_id: CurrentDataStore):
    """The providers this deployment knows, each with why it can't be connected (if it can't)
    and the organisation's connector to it (if any)."""
    with store_session(store_id, "ro") as store:
        have = dict(store.execute(select(Connector.provider, Connector.id)).all())
    out = []
    for p in source_providers.all_providers():
        why = source_providers.why_unavailable(p.name)
        if why is None and p.authorise_url is None:
            why = f"{p.title} can't be connected yet."
        out.append(
            ProviderOut(
                provider=p.name, title=p.title, unavailable=why, connector_id=have.get(p.name)
            )
        )
    return out


@router.get("/connectors", response_model=list[ConnectorOut], dependencies=[allow(Action.read)])
def list_connectors(store_id: CurrentDataStore):
    """The organisation's connectors, with their status, scope and health."""
    with store_session(store_id, "ro") as store:
        return _outs(store, list(store.scalars(select(Connector).order_by(Connector.id))))


@router.get(
    "/connectors/{connector_id}", response_model=ConnectorOut, dependencies=[allow(Action.read)]
)
def get_connector(connector_id: DbId, store_id: CurrentDataStore):
    with store_session(store_id, "ro") as store:
        return _one(store, connector_id)


def _start(request: Request, user, org, provider: str, connector_id: int | None) -> StartOut:
    found = _usable(provider)
    state = connectors.start_state(
        request.session, connectors.State(user.id, org.id, provider, connector_id)
    )
    return StartOut(redirect_url=found.authorise_url(state))


@router.post("/connectors", response_model=StartOut, dependencies=[allow(Action.sources)])
def start_connecting(
    body: StartIn,
    request: Request,
    user: CurrentUser,
    org: CurrentOrganisation,
    store_id: CurrentDataStore,
):
    """Start connecting a provider: where to send the browser to authorise it. 409 if the
    organisation has a connector to it already, or it can't be connected here."""
    _usable(body.provider)
    with store_session(store_id, "ro") as store:
        if store.scalar(select(Connector.id).where(Connector.provider == body.provider)):
            raise _already(body.provider)
    return _start(request, user, org, body.provider, None)


@router.post(
    "/connectors/{connector_id}/reconnect",
    response_model=StartOut,
    dependencies=[allow(Action.sources)],
)
def start_reconnecting(
    connector_id: DbId,
    request: Request,
    user: CurrentUser,
    org: CurrentOrganisation,
    store_id: CurrentDataStore,
):
    """Start reconnecting a connector whose access failed (or renewing one's): syncing carries on
    from where it stopped once it's done."""
    with store_session(store_id, "ro") as store:
        connector = _found(store, connector_id)
        provider = connector.provider
    return _start(request, user, org, provider, connector_id)


@router.get(CALLBACK.removeprefix("/api"))
def connect_callback(
    request: Request,
    user: CurrentUser,
    db: DB,
    state: str = Query(max_length=4096),
    code: str | None = Query(None, max_length=4096),
    error: str | None = Query(None, max_length=1024),
):
    """Where a provider sends the browser back. The state must be one aios signed (400), not
    expired (400), and started in this session by this person (403), who must still be an admin
    of its organisation (403). Lands on the connector's page."""
    try:
        found = connectors.read_state(state, request.session, user.id)
    except connectors.BadState as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except connectors.WrongSession as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    membership = organisations.membership_role(db, user.id, found.organisation_id)
    if membership is None or not can(membership[1], Action.sources):
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_ADMIN)
    org = membership[0]
    if error or not code:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{_title(found.provider)} didn't authorise aios."
        )
    provider = _usable(found.provider)
    store_id = data_store_for(org.id, db)
    db.commit()
    try:
        grant = provider.exchange(code)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{provider.title} didn't accept the authorisation."
        ) from exc
    with store_session(store_id, "rw") as store:
        if found.connector_id is None:
            connector = _connected(store, user.id, found.provider)
        else:
            connector = _reconnected(store, user.id, found.connector_id, found.provider)
        connector_id = connector.id
        with SessionLocal() as platform:
            connectors.keep_credential(
                platform, org.id, store_id, connector_id, found.provider, grant, user.id
            )
            platform.commit()
        source_id = store.scalar(select(Source.id).where(Source.connector_id == connector_id))
        store.commit()
    if found.connector_id is not None and source_id is not None:
        enqueue_sync(org.id, store_id, source_id, SyncKind.incremental)
    return RedirectResponse(
        f"/o/{org.slug}/data/connectors/{connector_id}", status.HTTP_303_SEE_OTHER
    )


def _connected(store: Session, user_id: int, provider: str) -> Connector:
    connector = Connector(provider=provider, name=_title(provider), created_by=user_id)
    store.add(connector)
    try:
        store.flush()
    except IntegrityError as exc:
        raise _already(provider) from exc
    audit.configured(store, user_id, "connector", connector, "connected", provider=provider)
    return connector


def _reconnected(store: Session, user_id: int, connector_id: int, provider: str) -> Connector:
    """The connector working again: its source resumes from its cursor, due at once."""
    connector = _locked(store, connector_id)
    if connector.provider != provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This isn't that connector's provider.")
    connector.status = ConnectorStatus.active
    source = store.scalar(
        select(Source).where(Source.connector_id == connector_id).with_for_update()
    )
    if source is not None and source.status in (SourceStatus.needs_reauth, SourceStatus.failing):
        source.status = SourceStatus.active
        source.failure_count = 0
        source.next_sync_at = None
    audit.configured(store, user_id, "connector", connector, "reconnected", provider=provider)
    return connector


@router.get(
    "/connectors/{connector_id}/tools",
    response_model=list[ToolOut],
    dependencies=[allow(Action.read)],
)
def connector_tools(connector_id: DbId, store_id: CurrentDataStore):
    """The tools the connector offers now (`aios.tools.connector`), for the agent editor's
    picker: 409 if it can't be asked (it needs reconnecting, say)."""
    with store_session(store_id, "ro") as store:
        connector = _found(store, connector_id)
        if connector.status == ConnectorStatus.needs_reauth:
            raise HTTPException(status.HTTP_409_CONFLICT, NEEDS_RECONNECTING)
        if why := source_providers.why_unavailable(connector.provider):
            raise HTTPException(status.HTTP_409_CONFLICT, why)
        try:
            offered = tool_kind.offered_now(store_id, connector)
        except tool_kind.Unavailable as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"This connector's tools can't be listed: {exc}"
            ) from exc
    return [
        ToolOut(
            name=t.name,
            description=t.description,
            input_schema=t.input_schema,
            read_only=t.read_only,
        )
        for t in offered
    ]


def _options(store_id, connector: Connector) -> list[source_providers.ScopeOption]:
    """The provider's scope options for the connector (409 if it can't be asked now)."""
    if connector.status == ConnectorStatus.needs_reauth:
        raise HTTPException(status.HTTP_409_CONFLICT, NEEDS_RECONNECTING)
    try:
        adapter = source_providers.adapter_for({"provider": connector.provider})
        secret = connectors.credential(store_id, connector.id)
        view = SourceView(0, connector.name, {"provider": connector.provider}, secret)
        return adapter.scope_options(view)
    except ProviderUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except NeedsReconnecting as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, NEEDS_RECONNECTING) from exc


@router.get(
    "/connectors/{connector_id}/scope-options",
    response_model=list[ScopeItemOut],
    dependencies=[allow(Action.sources)],
)
def scope_options(
    connector_id: DbId,
    store_id: CurrentDataStore,
    q: str = Query("", max_length=255),
):
    """What the connector can sync, matching `q`: items restricted at the source are marked."""
    with store_session(store_id, "ro") as store:
        connector = _found(store, connector_id)
        options = _options(store_id, connector)
    needle = q.strip().lower()
    return [
        ScopeItemOut(id=o.id, name=o.name, restricted=o.restricted)
        for o in options
        if needle in o.name.lower()
    ]


@router.put(
    "/connectors/{connector_id}/scope",
    response_model=ConnectorOut,
    dependencies=[allow(Action.sources)],
)
def set_scope(
    connector_id: DbId,
    body: ScopeIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    store_id: CurrentDataStore,
):
    """Choose what the connector syncs. 422 for something it can't sync, or for something
    restricted at its source without the acknowledgement. The first scope makes its source, which
    syncs at once; a changed one is synced in full next, removing what's no longer in it."""
    with store_session(store_id, "rw") as store:
        connector = _locked(store, connector_id)
        options = {o.id: o for o in _options(store_id, connector)}
        picked = list(dict.fromkeys(body.items))
        unknown = [i for i in picked if i not in options]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{connector.name} has nothing called {unknown[0]!r} to sync.",
            )
        restricted = [i for i in picked if options[i].restricted]
        if restricted and not body.acknowledge_restricted:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, connectors.RESTRICTED_WARNING
            )
        before = [i["id"] for i in connector.scope or []]
        connector.scope = [
            {"id": i, "name": options[i].name, "restricted": options[i].restricted} for i in picked
        ]
        source = store.scalar(
            select(Source).where(Source.connector_id == connector_id).with_for_update()
        )
        settings = {"provider": connector.provider, "scope": picked}
        if source is None:
            source = Source(
                kind=SourceKind.connector,
                name=connector.name,
                connector_id=connector_id,
                settings=settings,
                interval_seconds=get_settings().sync_interval_seconds,
                created_by=user.id,
            )
            store.add(source)
        elif set(before) != set(picked):
            source.settings = settings
            # Synced in full next, so what's left the scope goes.
            source.last_reconciled_at = None
            source.next_sync_at = None
        audit.configured(
            store,
            user.id,
            "connector",
            connector,
            "scope_changed",
            added=[i for i in picked if i not in before],
            removed=[i for i in before if i not in picked],
            restricted_acknowledged=bool(restricted),
        )
        store.commit()
        source_id = source.id
        changed = set(before) != set(picked) or not before
        status_now = source.status
        out = _one(store, connector_id)
    if changed and status_now == SourceStatus.active:
        enqueue_sync(org.id, store_id, source_id, SyncKind.incremental)
    return out


@router.delete(
    "/connectors/{connector_id}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[allow(Action.sources)],
)
def remove_connector(
    connector_id: DbId,
    background: BackgroundTasks,
    user: CurrentUser,
    store_id: CurrentDataStore,
    keep: bool = Query(description="Keep its content as Uploads documents (else delete it)."),
) -> None:
    """Remove the connector: it stops at once, its credentials are deleted (and revoked at the
    provider where possible), and its content is deleted, or (`keep`) kept as Uploads
    documents."""
    with store_session(store_id, "rw") as store:
        connector = _locked(store, connector_id)
        provider = connector.provider
        connectors.stop(store, connector, keep)
        audit.configured(store, user.id, "connector", connector, "removed", keep_documents=keep)
        store.commit()
    connectors.drop_credential(store_id, connector_id, provider)
    background.add_task(connectors.finish_removal, store_id, connector_id)
