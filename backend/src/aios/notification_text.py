"""What notifications say (`aios.notifications.describe`): each resolved, when it's listed, from
its reference in its organisation's store, every reference of a store read at once."""

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from aios.datastores.connections import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    store_session,
)
from aios.datastores.models import (
    Agent,
    Chat,
    ChatMessage,
    Document,
    Invocation,
    SchemaChangeRequest,
    Workflow,
)
from aios.models import (
    ChatAuthorKind,
    DataStore,
    Notification,
    Organisation,
    OrganisationMember,
    User,
)
from aios.notifications import (
    APPROVAL_NEEDED,
    CHAT_KINDS,
    MENTION,
    ORGANISATION_DELETION_CANCELLED,
    ORGANISATION_DELETION_SCHEDULED,
    RELEASED_DOCUMENT_CHANGED,
    REPLY,
    RUN_FAILED,
    Description,
    excerpt,
)

log = logging.getLogger(__name__)

APPROVAL_TITLE = "A schema change is waiting for you"
RELEASED_TITLE = "A document you released has changed"
RELEASED_DETAIL = "{name} is waiting for Jev to check it again, and isn't searchable until then."
SCHEDULED_TITLE = "{name} is scheduled for deletion."
SCHEDULED_DETAIL = "Its creator can cancel it until then."
CANCELLED_TITLE = "{name} will no longer be deleted."
CHAT_TITLES = {
    MENTION: "{author} mentioned you in {chat}",
    REPLY: "{author} replied in {chat}",
    RUN_FAILED: "{author} failed in {chat}",
}
RUN_FAILED_TITLE = "A run of {name} failed"
UNDELIVERED_TITLE = "{name}'s result wasn't delivered"


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def describe(
    db: Session, notifications: Sequence[Notification]
) -> dict[uuid.UUID, Description | None]:
    found: dict[uuid.UUID, Description | None] = {}
    if not notifications:
        return found
    org_ids = {n.organisation_id for n in notifications}
    orgs = {o.id: o for o in db.scalars(select(Organisation).where(Organisation.id.in_(org_ids)))}
    members = set(
        db.execute(
            select(OrganisationMember.organisation_id, OrganisationMember.user_id).where(
                OrganisationMember.organisation_id.in_(org_ids)
            )
        ).all()
    )
    stores = dict(
        db.execute(
            select(DataStore.owner_organisation_id, DataStore.id).where(
                DataStore.owner_organisation_id.in_(org_ids)
            )
        ).all()
    )
    in_store: dict[int, list[Notification]] = defaultdict(list)
    for n in notifications:
        org = orgs.get(n.organisation_id)
        if org is None or (n.organisation_id, n.user_id) not in members:
            found[n.id] = None
        elif n.kind == ORGANISATION_DELETION_SCHEDULED:
            found[n.id] = Description(SCHEDULED_TITLE.format(name=org.name), SCHEDULED_DETAIL)
        elif n.kind == ORGANISATION_DELETION_CANCELLED:
            found[n.id] = Description(CANCELLED_TITLE.format(name=org.name))
        else:
            in_store[n.organisation_id].append(n)
    for organisation_id, theirs in in_store.items():
        store_id = stores.get(organisation_id)
        if store_id is None:
            continue
        try:
            with store_session(store_id, "ro") as store:
                found.update(_in_store(db, store, theirs))
        except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy) as exc:
            log.info("couldn't read notifications' references in store %s: %s", store_id, exc)
    return found


def _names(db: Session, store: Session, keys: set[tuple[ChatAuthorKind, int]]) -> dict:
    names: dict[tuple[ChatAuthorKind, int], str] = {}
    for kind, model in ((ChatAuthorKind.agent, Agent), (ChatAuthorKind.workflow, Workflow)):
        ids = {i for k, i in keys if k == kind}
        if ids:
            for i, name in store.execute(select(model.id, model.name).where(model.id.in_(ids))):
                names[kind, i] = name
    people = {i for k, i in keys if k == ChatAuthorKind.user}
    if people:
        for user in db.scalars(select(User).where(User.id.in_(people))):
            names[ChatAuthorKind.user, user.id] = user.full_name
    return names


def _in_store(
    db: Session, store: Session, notifications: list[Notification]
) -> dict[uuid.UUID, Description | None]:
    found: dict[uuid.UUID, Description | None] = {}
    chat_kind = [n for n in notifications if n.kind in CHAT_KINDS and "chat_id" in n.ref]
    runs = [n for n in notifications if n.kind == RUN_FAILED and "run_id" in n.ref]
    approvals = [n for n in notifications if n.kind == APPROVAL_NEEDED]
    documents = [n for n in notifications if n.kind == RELEASED_DOCUMENT_CHANGED]
    known = {n.id for n in [*chat_kind, *runs, *approvals, *documents]}
    for n in notifications:
        if n.id not in known:
            # A kind this doesn't know: shown as it is.
            found[n.id] = Description(n.kind.replace("_", " ").capitalize())

    if chat_kind:
        message_ids = {_int(n.ref.get("message_id")) for n in chat_kind} - {None}
        rows = {
            m.id: (m, chat_name)
            for m, chat_name in store.execute(
                select(ChatMessage, Chat.name)
                .join(Chat, Chat.id == ChatMessage.chat_id)
                .where(ChatMessage.id.in_(message_ids))
            ).all()
        }
        authors = _names(
            db,
            store,
            {
                (m.author_kind, m.author_user_id or m.author_agent_id or m.author_workflow_id)
                for m, _ in rows.values()
            },
        )
        from aios.invocations import message_text

        for n in chat_kind:
            row = rows.get(_int(n.ref.get("message_id")))
            if row is None or row[0].chat_id != _int(n.ref.get("chat_id")):
                found[n.id] = None
                continue
            m, chat_name = row
            author_id = m.author_user_id or m.author_agent_id or m.author_workflow_id
            author = authors.get((m.author_kind, author_id), "Someone")
            found[n.id] = Description(
                CHAT_TITLES[n.kind].format(author=author, chat=chat_name),
                excerpt(message_text(m.body)) or None,
            )

    for n in runs:
        run_id = _int(n.ref.get("run_id"))
        if "agent_id" in n.ref:
            model, column, target = Agent, Invocation.agent_run_id, _int(n.ref.get("agent_id"))
        else:
            model, column = Workflow, Invocation.workflow_run_id
            target = _int(n.ref.get("workflow_id"))
        invocation = store.scalars(
            select(Invocation).where(column == run_id).order_by(Invocation.id).limit(1)
        ).one_or_none()
        target_row = store.get(model, target) if target is not None else None
        if invocation is None or target_row is None:
            found[n.id] = None
            continue
        if n.ref.get("undelivered"):
            # A delivery to a chat that couldn't happen (`aios.invocations._destination`).
            found[n.id] = Description(
                UNDELIVERED_TITLE.format(name=target_row.name),
                excerpt(invocation.delivery_error) if invocation.delivery_error else None,
            )
            continue
        found[n.id] = Description(
            RUN_FAILED_TITLE.format(name=target_row.name),
            excerpt(invocation.error) if invocation.error else None,
        )

    if approvals:
        ids = {_uuid(n.ref.get("approval_id")) for n in approvals} - {None}
        summaries = dict(
            store.execute(
                select(SchemaChangeRequest.id, SchemaChangeRequest.summary).where(
                    SchemaChangeRequest.id.in_(ids)
                )
            ).all()
        )
        for n in approvals:
            summary = summaries.get(_uuid(n.ref.get("approval_id")))
            found[n.id] = None if summary is None else Description(APPROVAL_TITLE, summary)

    if documents:
        ids = {_int(n.ref.get("document_id")) for n in documents} - {None}
        names = dict(
            store.execute(select(Document.id, Document.filename).where(Document.id.in_(ids))).all()
        )
        for n in documents:
            name = names.get(_int(n.ref.get("document_id")))
            found[n.id] = None
            if name is not None:
                found[n.id] = Description(RELEASED_TITLE, RELEASED_DETAIL.format(name=name))
    return found
