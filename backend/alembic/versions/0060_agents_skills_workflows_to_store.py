# ruff: noqa: E501 (the downgrade's DDL is as pg_dump writes it)
"""agents, skills and workflows move to the store

Agents, skills and workflows, with their versions, skill pins, tool settings, nodes and edges,
are now in each organisation's store (store revision 0009, `aios.datastores.models`), so their
tables here go. Nothing is copied: aios isn't live, and development data is rebuilt with
`make seed`.

What stays here and named one of them keeps its id, but no foreign key, since ids are the
store's own and repeat across organisations: agent and workflow runs (which now say whose
organisation they're in, `organisation_id`, filled in from what they ran), triggers, chat
members, chat messages' authors and chat invocations.

The downgrade brings the tables back empty, so it first deletes what names them here (runs,
triggers, chat members and invocations, and messages' agent and workflow authors are cleared).

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-11 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, constraint, column, referenced table, ON DELETE) of the foreign keys into the tables
# that move, from the tables that stay.
REFERENCES = [
    ("agent_runs", "agent_runs_agent_id_fkey", "agent_id", "agents", "CASCADE"),
    (
        "agent_runs",
        "agent_runs_agent_version_id_fkey",
        "agent_version_id",
        "agent_versions",
        "CASCADE",
    ),
    ("workflow_runs", "workflow_runs_workflow_id_fkey", "workflow_id", "workflows", "CASCADE"),
    (
        "workflow_runs",
        "workflow_runs_workflow_version_id_fkey",
        "workflow_version_id",
        "workflow_versions",
        "CASCADE",
    ),
    ("triggers", "triggers_agent_id_fkey", "agent_id", "agents", "CASCADE"),
    ("triggers", "triggers_workflow_id_fkey", "workflow_id", "workflows", "CASCADE"),
    ("chat_members", "chat_members_agent_id_fkey", "agent_id", "agents", "CASCADE"),
    ("chat_members", "chat_members_workflow_id_fkey", "workflow_id", "workflows", "CASCADE"),
    (
        "chat_messages",
        "chat_messages_author_agent_id_fkey",
        "author_agent_id",
        "agents",
        "SET NULL",
    ),
    (
        "chat_messages",
        "chat_messages_author_workflow_id_fkey",
        "author_workflow_id",
        "workflows",
        "SET NULL",
    ),
    ("chat_invocations", "chat_invocations_agent_id_fkey", "agent_id", "agents", "CASCADE"),
    (
        "chat_invocations",
        "chat_invocations_workflow_id_fkey",
        "workflow_id",
        "workflows",
        "CASCADE",
    ),
]

# The runs that now say whose organisation they're in, and what they ran.
RUNS = [("agent_runs", "agent_id", "agents"), ("workflow_runs", "workflow_id", "workflows")]

# What moves, those pinning others' rows first.
MOVED = [
    "workflow_edges",
    "workflow_nodes",
    "agent_version_tools",
    "agent_version_skills",
]
# Those whose current version names a version, which names them back.
VERSIONED = [
    ("workflows", "workflow_versions", "fk_workflows_current_version_id"),
    ("agents", "agent_versions", "fk_agents_current_version_id"),
    ("skills", "skill_versions", "fk_skills_current_version_id"),
]


def upgrade() -> None:
    for table, column, owner in RUNS:
        op.add_column(table, sa.Column("organisation_id", sa.Integer(), nullable=True))
        op.execute(
            f"UPDATE {table} r SET organisation_id = o.organisation_id FROM {owner} o"
            f" WHERE o.id = r.{column}"
        )
        op.alter_column(table, "organisation_id", nullable=False)
        op.create_foreign_key(
            f"{table}_organisation_id_fkey",
            table,
            "organisations",
            ["organisation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(f"ix_{table}_organisation_id", table, ["organisation_id"])
    for table, constraint, *_ in REFERENCES:
        op.drop_constraint(constraint, table, type_="foreignkey")
    for table, _, constraint in VERSIONED:
        op.drop_constraint(constraint, table, type_="foreignkey")
    for table in MOVED:
        op.drop_table(table)
    for table, versions, _ in VERSIONED:
        op.drop_table(versions)
        op.drop_table(table)


# The tables as they were (revision 0059), empty: their DDL as `pg_dump --schema-only` writes it.
TABLES = [
    """CREATE TABLE agent_version_skills (
        agent_version_id integer NOT NULL,
        skill_version_id integer NOT NULL,
        "position" integer NOT NULL
    )""",
    """CREATE TABLE agent_version_tools (
        agent_version_id integer NOT NULL,
        tool_key character varying(64) NOT NULL,
        config jsonb NOT NULL,
        "position" integer NOT NULL
    )""",
    """CREATE TABLE agent_versions (
        id integer NOT NULL,
        agent_id integer NOT NULL,
        version integer NOT NULL,
        base_prompt text NOT NULL,
        model character varying(255),
        instructions text NOT NULL,
        config_hash character varying(64) NOT NULL,
        note text,
        created_by integer,
        created_at timestamp with time zone DEFAULT now() NOT NULL
    )""",
    """CREATE SEQUENCE agent_versions_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1""",
    "ALTER SEQUENCE agent_versions_id_seq OWNED BY agent_versions.id",
    """CREATE TABLE agents (
        id integer NOT NULL,
        name character varying(255) NOT NULL,
        description text DEFAULT ''::text NOT NULL,
        current_version_id integer,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        builtin character varying(32),
        organisation_id integer NOT NULL,
        created_by integer,
        updated_by integer,
        archived_at timestamp with time zone,
        archived_by integer
    )""",
    """CREATE SEQUENCE agents_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1""",
    "ALTER SEQUENCE agents_id_seq OWNED BY agents.id",
    """CREATE TABLE skill_versions (
        id integer NOT NULL,
        skill_id integer NOT NULL,
        version integer NOT NULL,
        name character varying(255) NOT NULL,
        description text NOT NULL,
        content text NOT NULL,
        content_hash character varying(64) NOT NULL,
        note text,
        created_by integer,
        created_at timestamp with time zone DEFAULT now() NOT NULL
    )""",
    """CREATE SEQUENCE skill_versions_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1""",
    "ALTER SEQUENCE skill_versions_id_seq OWNED BY skill_versions.id",
    """CREATE TABLE skills (
        id integer NOT NULL,
        name character varying(255) NOT NULL,
        current_version_id integer,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        organisation_id integer NOT NULL,
        created_by integer,
        updated_by integer,
        archived_at timestamp with time zone,
        archived_by integer
    )""",
    """CREATE SEQUENCE skills_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1""",
    "ALTER SEQUENCE skills_id_seq OWNED BY skills.id",
    """CREATE TABLE workflow_edges (
        workflow_version_id integer NOT NULL,
        source character varying(64) NOT NULL,
        target character varying(64) NOT NULL,
        "position" integer NOT NULL
    )""",
    """CREATE TABLE workflow_nodes (
        workflow_version_id integer NOT NULL,
        key character varying(64) NOT NULL,
        kind workflow_node_kind NOT NULL,
        agent_version_id integer,
        subworkflow_version_id integer,
        "position" integer NOT NULL,
        CONSTRAINT ck_workflow_nodes_target CHECK ((((kind = 'agent'::workflow_node_kind) AND (agent_version_id IS NOT NULL) AND (subworkflow_version_id IS NULL)) OR ((kind = 'workflow'::workflow_node_kind) AND (subworkflow_version_id IS NOT NULL) AND (agent_version_id IS NULL))))
    )""",
    """CREATE TABLE workflow_versions (
        id integer NOT NULL,
        workflow_id integer NOT NULL,
        version integer NOT NULL,
        config_hash character varying(64) NOT NULL,
        note text,
        created_by integer,
        created_at timestamp with time zone DEFAULT now() NOT NULL
    )""",
    """CREATE SEQUENCE workflow_versions_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1""",
    "ALTER SEQUENCE workflow_versions_id_seq OWNED BY workflow_versions.id",
    """CREATE TABLE workflows (
        id integer NOT NULL,
        name character varying(255) NOT NULL,
        description text DEFAULT ''::text NOT NULL,
        current_version_id integer,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        layout jsonb DEFAULT '{}'::jsonb NOT NULL,
        organisation_id integer NOT NULL,
        created_by integer,
        updated_by integer,
        archived_at timestamp with time zone,
        archived_by integer
    )""",
    """CREATE SEQUENCE workflows_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1""",
    "ALTER SEQUENCE workflows_id_seq OWNED BY workflows.id",
    "ALTER TABLE agent_versions ALTER COLUMN id SET DEFAULT nextval('agent_versions_id_seq'::regclass)",
    "ALTER TABLE agents ALTER COLUMN id SET DEFAULT nextval('agents_id_seq'::regclass)",
    "ALTER TABLE skill_versions ALTER COLUMN id SET DEFAULT nextval('skill_versions_id_seq'::regclass)",
    "ALTER TABLE skills ALTER COLUMN id SET DEFAULT nextval('skills_id_seq'::regclass)",
    "ALTER TABLE workflow_versions ALTER COLUMN id SET DEFAULT nextval('workflow_versions_id_seq'::regclass)",
    "ALTER TABLE workflows ALTER COLUMN id SET DEFAULT nextval('workflows_id_seq'::regclass)",
    """ALTER TABLE agent_version_skills
        ADD CONSTRAINT agent_version_skills_pkey PRIMARY KEY (agent_version_id, skill_version_id)""",
    """ALTER TABLE agent_version_tools
        ADD CONSTRAINT agent_version_tools_pkey PRIMARY KEY (agent_version_id, tool_key)""",
    """ALTER TABLE agent_versions
        ADD CONSTRAINT agent_versions_agent_id_version_key UNIQUE (agent_id, version)""",
    """ALTER TABLE agent_versions
        ADD CONSTRAINT agent_versions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE agents
        ADD CONSTRAINT agents_pkey PRIMARY KEY (id)""",
    """ALTER TABLE skill_versions
        ADD CONSTRAINT skill_versions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE skill_versions
        ADD CONSTRAINT skill_versions_skill_id_version_key UNIQUE (skill_id, version)""",
    """ALTER TABLE skills
        ADD CONSTRAINT skills_pkey PRIMARY KEY (id)""",
    """ALTER TABLE workflow_edges
        ADD CONSTRAINT workflow_edges_pkey PRIMARY KEY (workflow_version_id, source, target)""",
    """ALTER TABLE workflow_nodes
        ADD CONSTRAINT workflow_nodes_pkey PRIMARY KEY (workflow_version_id, key)""",
    """ALTER TABLE workflow_versions
        ADD CONSTRAINT workflow_versions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE workflow_versions
        ADD CONSTRAINT workflow_versions_workflow_id_version_key UNIQUE (workflow_id, version)""",
    """ALTER TABLE workflows
        ADD CONSTRAINT workflows_pkey PRIMARY KEY (id)""",
    "CREATE INDEX ix_agent_version_skills_skill_version_id ON agent_version_skills USING btree (skill_version_id)",
    "CREATE INDEX ix_agents_organisation_id ON agents USING btree (organisation_id)",
    "CREATE INDEX ix_skills_organisation_id ON skills USING btree (organisation_id)",
    "CREATE INDEX ix_workflow_nodes_agent_version_id ON workflow_nodes USING btree (agent_version_id)",
    "CREATE INDEX ix_workflow_nodes_subworkflow_version_id ON workflow_nodes USING btree (subworkflow_version_id)",
    "CREATE INDEX ix_workflows_organisation_id ON workflows USING btree (organisation_id)",
    "CREATE UNIQUE INDEX uq_agents_organisation_id_builtin ON agents USING btree (organisation_id, builtin) WHERE (builtin IS NOT NULL)",
    "CREATE UNIQUE INDEX uq_agents_organisation_id_name ON agents USING btree (organisation_id, name)",
    "CREATE UNIQUE INDEX uq_skills_organisation_id_name ON skills USING btree (organisation_id, name)",
    "CREATE UNIQUE INDEX uq_workflows_organisation_id_name ON workflows USING btree (organisation_id, name)",
    """ALTER TABLE agent_version_skills
        ADD CONSTRAINT agent_version_skills_agent_version_id_fkey FOREIGN KEY (agent_version_id) REFERENCES agent_versions(id) ON DELETE CASCADE""",
    """ALTER TABLE agent_version_skills
        ADD CONSTRAINT agent_version_skills_skill_version_id_fkey FOREIGN KEY (skill_version_id) REFERENCES skill_versions(id)""",
    """ALTER TABLE agent_version_tools
        ADD CONSTRAINT agent_version_tools_agent_version_id_fkey FOREIGN KEY (agent_version_id) REFERENCES agent_versions(id) ON DELETE CASCADE""",
    """ALTER TABLE agent_versions
        ADD CONSTRAINT agent_versions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE""",
    """ALTER TABLE agent_versions
        ADD CONSTRAINT agent_versions_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE agents
        ADD CONSTRAINT agents_archived_by_fkey FOREIGN KEY (archived_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE agents
        ADD CONSTRAINT agents_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE agents
        ADD CONSTRAINT agents_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE""",
    """ALTER TABLE agents
        ADD CONSTRAINT agents_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE agents
        ADD CONSTRAINT fk_agents_current_version_id FOREIGN KEY (current_version_id) REFERENCES agent_versions(id)""",
    """ALTER TABLE skills
        ADD CONSTRAINT fk_skills_current_version_id FOREIGN KEY (current_version_id) REFERENCES skill_versions(id)""",
    """ALTER TABLE workflows
        ADD CONSTRAINT fk_workflows_current_version_id FOREIGN KEY (current_version_id) REFERENCES workflow_versions(id)""",
    """ALTER TABLE skill_versions
        ADD CONSTRAINT skill_versions_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE skill_versions
        ADD CONSTRAINT skill_versions_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE""",
    """ALTER TABLE skills
        ADD CONSTRAINT skills_archived_by_fkey FOREIGN KEY (archived_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE skills
        ADD CONSTRAINT skills_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE skills
        ADD CONSTRAINT skills_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE""",
    """ALTER TABLE skills
        ADD CONSTRAINT skills_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE workflow_edges
        ADD CONSTRAINT workflow_edges_workflow_version_id_fkey FOREIGN KEY (workflow_version_id) REFERENCES workflow_versions(id) ON DELETE CASCADE""",
    """ALTER TABLE workflow_edges
        ADD CONSTRAINT workflow_edges_workflow_version_id_source_fkey FOREIGN KEY (workflow_version_id, source) REFERENCES workflow_nodes(workflow_version_id, key) ON DELETE CASCADE""",
    """ALTER TABLE workflow_edges
        ADD CONSTRAINT workflow_edges_workflow_version_id_target_fkey FOREIGN KEY (workflow_version_id, target) REFERENCES workflow_nodes(workflow_version_id, key) ON DELETE CASCADE""",
    """ALTER TABLE workflow_nodes
        ADD CONSTRAINT workflow_nodes_agent_version_id_fkey FOREIGN KEY (agent_version_id) REFERENCES agent_versions(id) DEFERRABLE INITIALLY DEFERRED""",
    """ALTER TABLE workflow_nodes
        ADD CONSTRAINT workflow_nodes_subworkflow_version_id_fkey FOREIGN KEY (subworkflow_version_id) REFERENCES workflow_versions(id) DEFERRABLE INITIALLY DEFERRED""",
    """ALTER TABLE workflow_nodes
        ADD CONSTRAINT workflow_nodes_workflow_version_id_fkey FOREIGN KEY (workflow_version_id) REFERENCES workflow_versions(id) ON DELETE CASCADE""",
    """ALTER TABLE workflow_versions
        ADD CONSTRAINT workflow_versions_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE workflow_versions
        ADD CONSTRAINT workflow_versions_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE""",
    """ALTER TABLE workflows
        ADD CONSTRAINT workflows_archived_by_fkey FOREIGN KEY (archived_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE workflows
        ADD CONSTRAINT workflows_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",
    """ALTER TABLE workflows
        ADD CONSTRAINT workflows_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE""",
    """ALTER TABLE workflows
        ADD CONSTRAINT workflows_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL""",
]


def downgrade() -> None:
    # What names an agent or workflow goes, as they come back empty.
    op.execute("DELETE FROM triggers")
    op.execute("DELETE FROM chat_invocations")
    op.execute("DELETE FROM chat_members WHERE agent_id IS NOT NULL OR workflow_id IS NOT NULL")
    op.execute(
        "UPDATE chat_messages SET author_agent_id = NULL, author_workflow_id = NULL"
        " WHERE author_agent_id IS NOT NULL OR author_workflow_id IS NOT NULL"
    )
    op.execute("DELETE FROM workflow_runs")
    op.execute("DELETE FROM agent_runs")
    for statement in TABLES:
        op.execute(statement)
    for table, constraint, column, referenced, on_delete in REFERENCES:
        op.create_foreign_key(constraint, table, referenced, [column], ["id"], ondelete=on_delete)
    for table, *_ in RUNS:
        op.drop_index(f"ix_{table}_organisation_id", table_name=table)
        op.drop_column(table, "organisation_id")
