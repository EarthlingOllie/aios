# ruff: noqa: E501 (the downgrade's DDL is as pg_dump writes it)
"""schedules, chats, invocations and schema change requests move to the store

Schedules (`triggers`), chats with their members, messages and invocations, and schema change
requests are now in each organisation's store (store revision 0012, `aios.datastores.models`),
so their tables here go, with their enum types. Nothing is copied: aios isn't live, and
development data is rebuilt with `make seed`.

What stays is the schedule due index (`schedule_due`): for each enabled schedule, its store,
organisation and when it's next due, written whenever a schedule is saved or fires and rebuilt
from the stores' triggers by the repair sweep (`aios.schedules`). With it, the platform holds
no organisation content: only identity, organisations, memberships, invitations, the store
registry and its credentials, notifications (which name a request by its id and store, as they
did), the run history export's outbox and this index.

The downgrade brings the tables back empty, and drops the index.

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-12 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "chat_invocations",
    "chat_messages",
    "chat_members",
    "chats",
    "schema_change_requests",
    "triggers",
)
TYPES = (
    "chat_invocation_status",
    "chat_invocation_target_kind",
    "chat_message_kind",
    "chat_author_kind",
    "chat_member_role",
    "chat_member_kind",
    "chat_visibility",
    "schema_change_status",
    "trigger_status",
    "trigger_target_kind",
    "trigger_kind",
)


def upgrade() -> None:
    # The invocations' messages and the messages' invocations name each other.
    op.drop_constraint("fk_chat_messages_invocation_id", "chat_messages", type_="foreignkey")
    for table in TABLES:
        op.drop_table(table)
    for name in TYPES:
        op.execute(f"DROP TYPE {name}")
    op.create_table(
        "schedule_due",
        sa.Column(
            "data_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_stores.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("trigger_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column(
            "organisation_id",
            sa.Integer(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_schedule_due_next_run_at", "schedule_due", ["next_run_at"])
    op.create_index("ix_schedule_due_organisation_id", "schedule_due", ["organisation_id"])


DOWNGRADE = """
CREATE TYPE public.trigger_kind AS ENUM ('schedule');
CREATE TYPE public.trigger_target_kind AS ENUM ('agent', 'workflow');
CREATE TYPE public.trigger_status AS ENUM ('running', 'succeeded', 'failed');
CREATE TYPE public.chat_visibility AS ENUM ('public', 'private');
CREATE TYPE public.chat_member_kind AS ENUM ('user', 'agent', 'workflow');
CREATE TYPE public.chat_member_role AS ENUM ('owner', 'member');
CREATE TYPE public.schema_change_status AS ENUM ('pending', 'approved', 'rejected', 'expired', 'applied', 'failed', 'stale', 'cancelled');
CREATE TYPE public.chat_author_kind AS ENUM ('user', 'agent', 'workflow', 'system');
CREATE TYPE public.chat_message_kind AS ENUM ('text', 'run_card');
CREATE TYPE public.chat_invocation_target_kind AS ENUM ('agent', 'workflow');
CREATE TYPE public.chat_invocation_status AS ENUM ('queued', 'briefing', 'running', 'succeeded', 'failed');
CREATE TABLE public.chat_invocations (id integer NOT NULL, chat_id integer NOT NULL, message_id integer NOT NULL, requested_by integer, target_kind public.chat_invocation_target_kind NOT NULL, agent_id integer, workflow_id integer, status public.chat_invocation_status NOT NULL, error text, agent_run_id integer, workflow_run_id integer, created_at timestamp with time zone DEFAULT now() NOT NULL, started_at timestamp with time zone, finished_at timestamp with time zone, heartbeat_at timestamp with time zone, brief text, CONSTRAINT ck_chat_invocations_target CHECK ((((target_kind = 'agent'::public.chat_invocation_target_kind) AND (agent_id IS NOT NULL) AND (workflow_id IS NULL)) OR ((target_kind = 'workflow'::public.chat_invocation_target_kind) AND (workflow_id IS NOT NULL) AND (agent_id IS NULL)))));
CREATE SEQUENCE public.chat_invocations_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.chat_invocations_id_seq OWNED BY public.chat_invocations.id;
CREATE TABLE public.chat_members (id integer NOT NULL, chat_id integer NOT NULL, kind public.chat_member_kind NOT NULL, user_id integer, agent_id integer, workflow_id integer, role public.chat_member_role, added_by integer, added_at timestamp with time zone DEFAULT now() NOT NULL, last_read_message_id integer, CONSTRAINT ck_chat_members_role CHECK (((kind = 'user'::public.chat_member_kind) = (role IS NOT NULL))), CONSTRAINT ck_chat_members_target CHECK ((((kind = 'user'::public.chat_member_kind) AND (user_id IS NOT NULL) AND (agent_id IS NULL) AND (workflow_id IS NULL)) OR ((kind = 'agent'::public.chat_member_kind) AND (agent_id IS NOT NULL) AND (user_id IS NULL) AND (workflow_id IS NULL)) OR ((kind = 'workflow'::public.chat_member_kind) AND (workflow_id IS NOT NULL) AND (user_id IS NULL) AND (agent_id IS NULL)))));
CREATE SEQUENCE public.chat_members_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.chat_members_id_seq OWNED BY public.chat_members.id;
CREATE TABLE public.chat_messages (id integer NOT NULL, chat_id integer NOT NULL, author_kind public.chat_author_kind NOT NULL, author_user_id integer, author_agent_id integer, author_workflow_id integer, kind public.chat_message_kind NOT NULL, body text NOT NULL, mentions jsonb DEFAULT '[]'::jsonb NOT NULL, created_at timestamp with time zone DEFAULT now() NOT NULL, invocation_id integer, CONSTRAINT ck_chat_messages_author CHECK ((((author_kind = 'user'::public.chat_author_kind) OR (author_user_id IS NULL)) AND ((author_kind = 'agent'::public.chat_author_kind) OR (author_agent_id IS NULL)) AND ((author_kind = 'workflow'::public.chat_author_kind) OR (author_workflow_id IS NULL)))), CONSTRAINT ck_chat_messages_run_card CHECK (((kind <> 'run_card'::public.chat_message_kind) OR (invocation_id IS NOT NULL))));
CREATE SEQUENCE public.chat_messages_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.chat_messages_id_seq OWNED BY public.chat_messages.id;
CREATE TABLE public.chats (id integer NOT NULL, name character varying(255) NOT NULL, description text DEFAULT ''::text NOT NULL, visibility public.chat_visibility NOT NULL, created_at timestamp with time zone DEFAULT now() NOT NULL, updated_at timestamp with time zone DEFAULT now() NOT NULL, organisation_id integer NOT NULL, created_by integer, updated_by integer);
CREATE SEQUENCE public.chats_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.chats_id_seq OWNED BY public.chats.id;
CREATE TABLE public.schema_change_requests (id uuid DEFAULT gen_random_uuid() NOT NULL, data_store_id uuid NOT NULL, ops jsonb NOT NULL, sql text NOT NULL, summary text NOT NULL, reason text NOT NULL, requested_by_run integer, tool_call_id character varying(255), base_revision bigint NOT NULL, status public.schema_change_status DEFAULT 'pending'::public.schema_change_status NOT NULL, decided_by_user_id integer, decided_at timestamp with time zone, note text, applied_at timestamp with time zone, error text, resumed_at timestamp with time zone, created_at timestamp with time zone DEFAULT now() NOT NULL);
CREATE TABLE public.triggers (id integer NOT NULL, kind public.trigger_kind NOT NULL, target_kind public.trigger_target_kind NOT NULL, agent_id integer, workflow_id integer, enabled boolean DEFAULT true NOT NULL, input text DEFAULT ''::text NOT NULL, cron character varying(255), timezone character varying(64), next_run_at timestamp with time zone, last_fired_at timestamp with time zone, last_status public.trigger_status, last_error text, heartbeat_at timestamp with time zone, last_run_id integer, last_workflow_run_id integer, created_at timestamp with time zone DEFAULT now() NOT NULL, updated_at timestamp with time zone DEFAULT now() NOT NULL, organisation_id integer NOT NULL, created_by integer, updated_by integer, CONSTRAINT ck_triggers_schedule CHECK (((kind <> 'schedule'::public.trigger_kind) OR ((cron IS NOT NULL) AND (timezone IS NOT NULL)))), CONSTRAINT ck_triggers_target CHECK ((((target_kind = 'agent'::public.trigger_target_kind) AND (agent_id IS NOT NULL) AND (workflow_id IS NULL)) OR ((target_kind = 'workflow'::public.trigger_target_kind) AND (workflow_id IS NOT NULL) AND (agent_id IS NULL)))));
CREATE SEQUENCE public.triggers_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.triggers_id_seq OWNED BY public.triggers.id;
ALTER TABLE ONLY public.chat_invocations ALTER COLUMN id SET DEFAULT nextval('public.chat_invocations_id_seq'::regclass);
ALTER TABLE ONLY public.chat_members ALTER COLUMN id SET DEFAULT nextval('public.chat_members_id_seq'::regclass);
ALTER TABLE ONLY public.chat_messages ALTER COLUMN id SET DEFAULT nextval('public.chat_messages_id_seq'::regclass);
ALTER TABLE ONLY public.chats ALTER COLUMN id SET DEFAULT nextval('public.chats_id_seq'::regclass);
ALTER TABLE ONLY public.triggers ALTER COLUMN id SET DEFAULT nextval('public.triggers_id_seq'::regclass);
ALTER TABLE ONLY public.chat_invocations ADD CONSTRAINT chat_invocations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.chat_members ADD CONSTRAINT chat_members_chat_id_agent_id_key UNIQUE (chat_id, agent_id);
ALTER TABLE ONLY public.chat_members ADD CONSTRAINT chat_members_chat_id_user_id_key UNIQUE (chat_id, user_id);
ALTER TABLE ONLY public.chat_members ADD CONSTRAINT chat_members_chat_id_workflow_id_key UNIQUE (chat_id, workflow_id);
ALTER TABLE ONLY public.chat_members ADD CONSTRAINT chat_members_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.chat_messages ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.chats ADD CONSTRAINT chats_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.schema_change_requests ADD CONSTRAINT schema_change_requests_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.triggers ADD CONSTRAINT triggers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.schema_change_requests ADD CONSTRAINT uq_schema_change_requests_run_tool_call UNIQUE (data_store_id, requested_by_run, tool_call_id);
CREATE INDEX ix_chat_invocations_agent_id ON public.chat_invocations USING btree (agent_id);
CREATE INDEX ix_chat_invocations_agent_run_id ON public.chat_invocations USING btree (agent_run_id);
CREATE INDEX ix_chat_invocations_chat_id ON public.chat_invocations USING btree (chat_id);
CREATE INDEX ix_chat_invocations_message_id ON public.chat_invocations USING btree (message_id);
CREATE INDEX ix_chat_invocations_requested_by ON public.chat_invocations USING btree (requested_by);
CREATE INDEX ix_chat_invocations_unfinished ON public.chat_invocations USING btree (chat_id) WHERE (status = ANY (ARRAY['queued'::public.chat_invocation_status, 'briefing'::public.chat_invocation_status, 'running'::public.chat_invocation_status]));
CREATE INDEX ix_chat_invocations_workflow_id ON public.chat_invocations USING btree (workflow_id);
CREATE INDEX ix_chat_invocations_workflow_run_id ON public.chat_invocations USING btree (workflow_run_id);
CREATE INDEX ix_chat_members_agent_id ON public.chat_members USING btree (agent_id);
CREATE INDEX ix_chat_members_chat_id ON public.chat_members USING btree (chat_id);
CREATE INDEX ix_chat_members_user_id ON public.chat_members USING btree (user_id);
CREATE INDEX ix_chat_members_workflow_id ON public.chat_members USING btree (workflow_id);
CREATE INDEX ix_chat_messages_author_agent_id ON public.chat_messages USING btree (author_agent_id);
CREATE INDEX ix_chat_messages_author_user_id ON public.chat_messages USING btree (author_user_id);
CREATE INDEX ix_chat_messages_author_workflow_id ON public.chat_messages USING btree (author_workflow_id);
CREATE INDEX ix_chat_messages_chat_id_id ON public.chat_messages USING btree (chat_id, id);
CREATE INDEX ix_chat_messages_invocation_id ON public.chat_messages USING btree (invocation_id);
CREATE INDEX ix_chats_organisation_id ON public.chats USING btree (organisation_id);
CREATE INDEX ix_schema_change_requests_data_store_id ON public.schema_change_requests USING btree (data_store_id);
CREATE INDEX ix_schema_change_requests_requested_by_run ON public.schema_change_requests USING btree (requested_by_run);
CREATE INDEX ix_triggers_agent_id ON public.triggers USING btree (agent_id);
CREATE INDEX ix_triggers_due ON public.triggers USING btree (next_run_at) WHERE (next_run_at IS NOT NULL);
CREATE INDEX ix_triggers_organisation_id ON public.triggers USING btree (organisation_id);
CREATE INDEX ix_triggers_workflow_id ON public.triggers USING btree (workflow_id);
CREATE UNIQUE INDEX uq_chat_members_chat_id_owner ON public.chat_members USING btree (chat_id) WHERE (role = 'owner'::public.chat_member_role);
CREATE UNIQUE INDEX uq_chats_organisation_id_name ON public.chats USING btree (organisation_id, name);
ALTER TABLE ONLY public.chat_invocations ADD CONSTRAINT chat_invocations_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.chat_invocations ADD CONSTRAINT chat_invocations_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.chat_messages(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.chat_invocations ADD CONSTRAINT chat_invocations_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.chat_members ADD CONSTRAINT chat_members_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.chat_members ADD CONSTRAINT chat_members_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.chat_members ADD CONSTRAINT chat_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.chat_messages ADD CONSTRAINT chat_messages_author_user_id_fkey FOREIGN KEY (author_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.chat_messages ADD CONSTRAINT chat_messages_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.chats ADD CONSTRAINT chats_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.chats ADD CONSTRAINT chats_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.chats ADD CONSTRAINT chats_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.chat_messages ADD CONSTRAINT fk_chat_messages_invocation_id FOREIGN KEY (invocation_id) REFERENCES public.chat_invocations(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.schema_change_requests ADD CONSTRAINT schema_change_requests_data_store_id_fkey FOREIGN KEY (data_store_id) REFERENCES public.data_stores(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.schema_change_requests ADD CONSTRAINT schema_change_requests_decided_by_user_id_fkey FOREIGN KEY (decided_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.triggers ADD CONSTRAINT triggers_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.triggers ADD CONSTRAINT triggers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.triggers ADD CONSTRAINT triggers_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;
"""


def downgrade() -> None:
    op.drop_table("schedule_due")
    op.execute(DOWNGRADE)
