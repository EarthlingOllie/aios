# ruff: noqa: E501 (the downgrade's DDL is as pg_dump writes it)
"""runs move to the store

Agent runs, their sandbox sessions, workflow runs and their steps are now in each organisation's
store (store revision 0010, `aios.datastores.models`), as are the runs' checkpoints, so their
tables here go, with the checkpointer's schema (`langgraph`) and the app role's search path that
found it (migration 0057). Nothing is copied: aios isn't live, and development data is rebuilt
with `make seed`.

What stays here and named a run keeps its id, but no foreign key, since ids are the store's own
and repeat across organisations: schedules (their last runs), chat invocations (the run each
started) and schema change requests (the run that asked, whose tool call is now unique within
the request's store, not across every store). Each is cleared when its run is deleted.

The downgrade brings the tables back empty (and the search path); the checkpointer makes its
schema again on first use. So it first clears what names a run here.

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-11 22:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, constraint, column, referenced table) of the foreign keys into the tables that go, from
# the tables that stay; all ON DELETE SET NULL.
REFERENCES = [
    ("triggers", "fk_triggers_last_run_id", "last_run_id", "agent_runs"),
    ("triggers", "triggers_last_workflow_run_id_fkey", "last_workflow_run_id", "workflow_runs"),
    (
        "schema_change_requests",
        "schema_change_requests_requested_by_run_fkey",
        "requested_by_run",
        "agent_runs",
    ),
    ("chat_invocations", "chat_invocations_agent_run_id_fkey", "agent_run_id", "agent_runs"),
    (
        "chat_invocations",
        "chat_invocations_workflow_run_id_fkey",
        "workflow_run_id",
        "workflow_runs",
    ),
]

TABLES = ("sandbox_sessions", "workflow_run_steps", "workflow_runs", "agent_runs")
TYPES = ("agent_run_status", "workflow_run_status", "workflow_step_status", "workflow_node_kind")


def _alter_role(action: str) -> None:
    op.execute(
        "DO $body$ BEGIN EXECUTE 'ALTER ROLE CURRENT_USER IN DATABASE '"
        f" || quote_ident(current_database()) || ' {action}'; END $body$"
    )


def upgrade() -> None:
    for table, constraint, *_ in REFERENCES:
        op.drop_constraint(constraint, table, type_="foreignkey")
    # Ids repeat across stores: a run's tool call is unique within its store, and two chats'
    # invocations may name runs with the same id in different organisations.
    op.drop_constraint(
        "schema_change_requests_requested_by_run_tool_call_id_key",
        "schema_change_requests",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_schema_change_requests_run_tool_call",
        "schema_change_requests",
        ["data_store_id", "requested_by_run", "tool_call_id"],
    )
    for column in ("agent_run_id", "workflow_run_id"):
        op.drop_constraint(f"chat_invocations_{column}_key", "chat_invocations", type_="unique")
        op.create_index(f"ix_chat_invocations_{column}", "chat_invocations", [column])
    for table in TABLES:
        op.drop_table(table)
    for name in TYPES:
        op.execute(f"DROP TYPE {name}")
    # The checkpointer's schema, and the search path that found it.
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
    _alter_role("RESET search_path")


DOWNGRADE = """
CREATE TYPE public.agent_run_status AS ENUM ('running', 'succeeded', 'failed');
CREATE TYPE public.workflow_node_kind AS ENUM ('agent', 'workflow');
CREATE TYPE public.workflow_run_status AS ENUM ('queued', 'running', 'waiting', 'succeeded', 'failed');
CREATE TYPE public.workflow_step_status AS ENUM ('running', 'waiting', 'succeeded', 'failed');
CREATE TABLE public.agent_runs (
    id integer NOT NULL,
    agent_id integer NOT NULL,
    agent_version_id integer NOT NULL,
    title character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    message_count integer DEFAULT 0 NOT NULL,
    trigger_id integer,
    status public.agent_run_status,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    tool_call_count integer,
    input_tokens integer,
    output_tokens integer,
    heartbeat_at timestamp with time zone,
    created_by integer,
    lease_token uuid,
    lease_until timestamp with time zone,
    organisation_id integer NOT NULL
);
CREATE SEQUENCE public.agent_runs_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.agent_runs_id_seq OWNED BY public.agent_runs.id;
CREATE TABLE public.sandbox_sessions (
    id integer NOT NULL,
    agent_run_id integer NOT NULL,
    ref character varying(512) NOT NULL,
    endpoint character varying(512) NOT NULL,
    secret character varying(255) NOT NULL,
    replaced boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.sandbox_sessions_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.sandbox_sessions_id_seq OWNED BY public.sandbox_sessions.id;
CREATE TABLE public.workflow_run_steps (
    id integer NOT NULL,
    run_id integer NOT NULL,
    path character varying(64)[] NOT NULL,
    kind public.workflow_node_kind NOT NULL,
    status public.workflow_step_status NOT NULL,
    input text NOT NULL,
    output text,
    error text,
    agent_run_id integer,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    waiting_on_request_id uuid
);
CREATE SEQUENCE public.workflow_run_steps_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.workflow_run_steps_id_seq OWNED BY public.workflow_run_steps.id;
CREATE TABLE public.workflow_runs (
    id integer NOT NULL,
    workflow_id integer NOT NULL,
    workflow_version_id integer NOT NULL,
    status public.workflow_run_status NOT NULL,
    input text NOT NULL,
    output text,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    waiting_since timestamp with time zone,
    waited_seconds double precision DEFAULT 0 NOT NULL,
    created_by integer,
    trigger_id integer,
    organisation_id integer NOT NULL
);
CREATE SEQUENCE public.workflow_runs_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.workflow_runs_id_seq OWNED BY public.workflow_runs.id;
ALTER TABLE ONLY public.agent_runs ALTER COLUMN id SET DEFAULT nextval('public.agent_runs_id_seq'::regclass);
ALTER TABLE ONLY public.sandbox_sessions ALTER COLUMN id SET DEFAULT nextval('public.sandbox_sessions_id_seq'::regclass);
ALTER TABLE ONLY public.workflow_run_steps ALTER COLUMN id SET DEFAULT nextval('public.workflow_run_steps_id_seq'::regclass);
ALTER TABLE ONLY public.workflow_runs ALTER COLUMN id SET DEFAULT nextval('public.workflow_runs_id_seq'::regclass);
ALTER TABLE ONLY public.agent_runs ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.sandbox_sessions ADD CONSTRAINT sandbox_sessions_agent_run_id_key UNIQUE (agent_run_id);
ALTER TABLE ONLY public.sandbox_sessions ADD CONSTRAINT sandbox_sessions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.workflow_run_steps ADD CONSTRAINT workflow_run_steps_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.workflow_runs ADD CONSTRAINT workflow_runs_pkey PRIMARY KEY (id);
CREATE INDEX ix_agent_runs_agent_id ON public.agent_runs USING btree (agent_id);
CREATE INDEX ix_agent_runs_agent_version_id ON public.agent_runs USING btree (agent_version_id);
CREATE INDEX ix_agent_runs_organisation_id ON public.agent_runs USING btree (organisation_id);
CREATE INDEX ix_agent_runs_running_heartbeat ON public.agent_runs USING btree (heartbeat_at) WHERE ((status = 'running'::public.agent_run_status) AND (heartbeat_at IS NOT NULL));
CREATE INDEX ix_agent_runs_trigger_id ON public.agent_runs USING btree (trigger_id);
CREATE INDEX ix_workflow_run_steps_agent_run_id ON public.workflow_run_steps USING btree (agent_run_id);
CREATE INDEX ix_workflow_run_steps_run_id ON public.workflow_run_steps USING btree (run_id);
CREATE INDEX ix_workflow_run_steps_waiting_on_request_id ON public.workflow_run_steps USING btree (waiting_on_request_id);
CREATE INDEX ix_workflow_runs_organisation_id ON public.workflow_runs USING btree (organisation_id);
CREATE INDEX ix_workflow_runs_trigger_id ON public.workflow_runs USING btree (trigger_id);
CREATE INDEX ix_workflow_runs_workflow_id ON public.workflow_runs USING btree (workflow_id);
CREATE INDEX ix_workflow_runs_workflow_version_id ON public.workflow_runs USING btree (workflow_version_id);
ALTER TABLE ONLY public.agent_runs ADD CONSTRAINT agent_runs_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.agent_runs ADD CONSTRAINT agent_runs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.agent_runs ADD CONSTRAINT agent_runs_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES public.triggers(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.sandbox_sessions ADD CONSTRAINT sandbox_sessions_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES public.agent_runs(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_run_steps ADD CONSTRAINT workflow_run_steps_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES public.agent_runs(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.workflow_run_steps ADD CONSTRAINT workflow_run_steps_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.workflow_runs(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_run_steps ADD CONSTRAINT workflow_run_steps_waiting_on_request_id_fkey FOREIGN KEY (waiting_on_request_id) REFERENCES public.schema_change_requests(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.workflow_runs ADD CONSTRAINT workflow_runs_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.workflow_runs ADD CONSTRAINT workflow_runs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_runs ADD CONSTRAINT workflow_runs_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES public.triggers(id) ON DELETE SET NULL;
"""


def downgrade() -> None:
    _alter_role("""SET search_path = "$user", public, langgraph""")
    # What names a run goes, as the runs come back empty.
    op.execute("UPDATE triggers SET last_run_id = NULL, last_workflow_run_id = NULL")
    op.execute("UPDATE schema_change_requests SET requested_by_run = NULL")
    op.execute("UPDATE chat_invocations SET agent_run_id = NULL, workflow_run_id = NULL")
    op.execute(DOWNGRADE)
    for column in ("agent_run_id", "workflow_run_id"):
        op.drop_index(f"ix_chat_invocations_{column}", table_name="chat_invocations")
        op.create_unique_constraint(f"chat_invocations_{column}_key", "chat_invocations", [column])
    op.drop_constraint(
        "uq_schema_change_requests_run_tool_call", "schema_change_requests", type_="unique"
    )
    op.create_unique_constraint(
        "schema_change_requests_requested_by_run_tool_call_id_key",
        "schema_change_requests",
        ["requested_by_run", "tool_call_id"],
    )
    for table, constraint, column, referenced in REFERENCES:
        op.create_foreign_key(constraint, table, referenced, [column], ["id"], ondelete="SET NULL")
