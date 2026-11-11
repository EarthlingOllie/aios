"""Rebuild development data from scratch: `make seed` (`python -m aios.seed`).

aios isn't live, so a change to the schema keeps nothing for older rows: the database is
migrated to head and its data rebuilt instead. This wipes every organisation, with its store
(database, roles and files), every user and everything they made, then makes one working
organisation through the app's own writers (the API's handlers, called as a request would call
them), so what it makes is what the app would have made:

- the dev user, `dev@example.com`, whose password is `SEED_PASSWORD`, in their organisation, with
  its store provisioned;
- in the store, a skill, agents that use it, and a workflow that runs one of them inside another
  workflow; and a schedule;
- a chat with the documents assistant and an agent in it, and a first message, which posting
  indexes for search as it does any (`aios.search_index`);
- an archived agent, to show what archiving does.

It refuses to run unless the development sign-in picker is on (`DEV_LOGIN_ENABLED`), which no
deployment has, or `--force` is given.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from fastapi import BackgroundTasks
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from aios import organisations
from aios.api.agents import AgentIn, archive_agent, create_agent
from aios.api.chats import ChatIn, MemberIn, MessageIn, add_member, create_chat, post_message
from aios.api.skills import SkillIn, create_skill
from aios.api.triggers import TriggerIn, create_trigger
from aios.api.workflows import WorkflowEdgeIn, WorkflowIn, WorkflowNodeIn, create_workflow
from aios.config import get_settings
from aios.datastores import data_store_for, organisation_store, provision
from aios.db import SessionLocal
from aios.models import (
    ChatMemberKind,
    ChatVisibility,
    DataStore,
    DataStoreStatus,
    Organisation,
    Role,
    TriggerTargetKind,
    User,
    WorkflowNodeKind,
)
from aios.passwords import hash_password

EMAIL = "dev@example.com"
SEED_PASSWORD = "development only"
BACKEND = Path(__file__).resolve().parents[2]


def migrate() -> None:
    """Bring the platform database to head, as the deploy's `migrate` does."""
    subprocess.run(["alembic", "upgrade", "head"], cwd=BACKEND, check=True)


def wipe(db: Session) -> None:
    """Delete every store (its database, roles and files: its runs and their history with it)
    and every row of the platform database but its migration revision."""
    stores = db.scalars(select(DataStore.id).where(DataStore.status != DataStoreStatus.deleted))
    ids = list(stores)
    db.execute(
        update(DataStore).where(DataStore.id.in_(ids)).values(status=DataStoreStatus.deleting)
    )
    db.commit()
    for store_id in ids:
        provision.delete_store(store_id)
    tables = db.scalars(
        text(
            "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables"
            " WHERE schemaname = 'public' AND tableowner = current_user"
            " AND tablename <> 'alembic_version'"
        )
    ).all()
    if tables:
        db.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    db.commit()


def build(db: Session) -> None:
    """One working organisation, made as its member would make it."""
    user = User(
        first_name="Dev",
        last_name="Tester",
        email=EMAIL,
        password_hash=hash_password(SEED_PASSWORD),
    )
    db.add(user)
    db.flush()
    org = organisations.create(db, user, "Dev Team")
    db.commit()
    provision.provision(data_store_for(org.id))
    with organisation_store(org.id) as store:
        build_contents(db, store, user, org)


def build_contents(db: Session, store: Session, user: User, org: Organisation) -> None:
    """What the organisation has, made through the API's handlers: its agents, skills and
    workflows, its schedule and its chat in its store (`store`)."""
    skill = create_skill(
        SkillIn(
            name="Plain English",
            description="Write so anyone can follow",
            content="Use short sentences and everyday words.",
        ),
        user,
        org,
        db,
        store,
    )
    version = skill.current_version.id

    def agent(body: AgentIn):
        return create_agent(body, user, org, db, store)

    writer = agent(
        AgentIn(
            name="Writer",
            description="Drafts notes and summaries",
            base_prompt="You write clear, short drafts.",
            skill_version_ids=[version],
        )
    )
    reviewer = agent(
        AgentIn(
            name="Reviewer",
            description="Checks a draft and suggests changes",
            base_prompt="You review drafts for clarity and accuracy.",
            skill_version_ids=[version],
        )
    )
    old = agent(
        AgentIn(name="Old researcher", description="Put away: archived agents keep their history")
    )

    def node(key: str, kind: WorkflowNodeKind, version_id: int) -> WorkflowNodeIn:
        return WorkflowNodeIn(key=key, kind=kind, version_id=version_id)

    draft = create_workflow(
        WorkflowIn(
            name="Draft and review",
            nodes=[
                node("draft", WorkflowNodeKind.agent, writer.current_version.id),
                node("review", WorkflowNodeKind.agent, reviewer.current_version.id),
            ],
            edges=[WorkflowEdgeIn(source="draft", target="review")],
        ),
        user,
        org,
        db,
        store,
    )
    create_workflow(
        WorkflowIn(
            name="Weekly digest",
            nodes=[node("digest", WorkflowNodeKind.workflow, draft.current_version.id)],
        ),
        user,
        org,
        db,
        store,
    )
    create_trigger(
        TriggerIn(
            target_kind=TriggerTargetKind.agent,
            target_id=writer.id,
            cron="0 9 * * 1",
            timezone="Europe/London",
            input="Summarise last week's notes.",
        ),
        user,
        org,
        db,
        store,
    )
    chat = create_chat(
        ChatIn(name="General", visibility=ChatVisibility.public), user, org, db, store
    )
    for member in (writer, old):
        add_member(chat.id, MemberIn(kind=ChatMemberKind.agent, id=member.id), user, org, db, store)
    archive_agent(old.id, user, org, db, store)
    post_message(
        chat.id,
        MessageIn(body="Welcome! Our weekly notes are summarised here every Monday."),
        user,
        org,
        Role.admin,
        db,
        store,
        BackgroundTasks(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="run without DEV_LOGIN_ENABLED")
    args = parser.parse_args(argv)
    if not get_settings().dev_login_enabled and not args.force:
        print(
            "Refusing to wipe the database: DEV_LOGIN_ENABLED is off, so this may not be a"
            " development database. Pass --force if it is.",
            file=sys.stderr,
        )
        return 1
    migrate()
    with SessionLocal() as db:
        wipe(db)
        build(db)
    print(f"Seeded. Sign in as {EMAIL} with the password {SEED_PASSWORD!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
