"""agents, skills and workflows

What the organisation builds moves into its store: skills, agents and workflows, each with its
versions (`skill_versions`, `agent_versions`, `workflow_versions`), the skill versions each agent
version pins (`agent_version_skills`) and the tools it has (`agent_version_tools`), and each
workflow version's nodes and edges. Ids are the store's own. Who made, changed, archived or saved
each is a platform user id, resolved through the people mirror; a store has no users table, so
there's no foreign key. Nothing is copied from the platform, whose tables the same release drops:
development data is rebuilt with `make seed`.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def _now() -> sa.Column:
    return sa.text("now()")


def _provenance() -> list[sa.Column]:
    # Who made it and who last changed it: platform user ids.
    return [
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
    ]


def _archived() -> list[sa.Column]:
    return [
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.Integer(), nullable=True),
    ]


def _times(*, updated: bool = True) -> list[sa.Column]:
    columns = [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False)
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
            )
        )
    return columns


def upgrade() -> None:
    # Skills.
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True),
        *_provenance(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        *_archived(),
        *_times(),
        schema=SCHEMA,
        comment="Skills: reusable instructions agents can be given",
    )
    op.create_index("uq_skills_name", "skills", ["name"], unique=True, schema=SCHEMA)
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey(f"{SCHEMA}.skills.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        *_times(updated=False),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_id_version"),
        schema=SCHEMA,
        comment="Each skill's versions, which never change once saved",
    )
    op.create_foreign_key(
        "fk_skills_current_version_id",
        "skills",
        "skill_versions",
        ["current_version_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )

    # Agents.
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        *_provenance(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.Column("builtin", sa.String(32), nullable=True),
        *_archived(),
        *_times(),
        schema=SCHEMA,
        comment="Agents: each a name, and its current version",
    )
    op.create_index("uq_agents_name", "agents", ["name"], unique=True, schema=SCHEMA)
    op.create_index(
        "uq_agents_builtin",
        "agents",
        ["builtin"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("builtin IS NOT NULL"),
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey(f"{SCHEMA}.agents.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        *_times(updated=False),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_id_version"),
        schema=SCHEMA,
        comment="Each agent's versions, which never change once saved",
    )
    op.create_foreign_key(
        "fk_agents_current_version_id",
        "agents",
        "agent_versions",
        ["current_version_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_table(
        "agent_version_skills",
        sa.Column(
            "agent_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_versions.id"),
            primary_key=True,
        ),
        sa.Column(
            "skill_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.skill_versions.id"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        schema=SCHEMA,
        comment="The skill versions each agent version pins, in order",
    )
    op.create_index(
        "ix_agent_version_skills_skill_version_id",
        "agent_version_skills",
        ["skill_version_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "agent_version_tools",
        sa.Column(
            "agent_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_versions.id"),
            primary_key=True,
        ),
        sa.Column("tool_key", sa.String(64), primary_key=True),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        schema=SCHEMA,
        comment="The tools each agent version has, in order, with their settings",
    )

    # Workflows.
    op.create_table(
        "workflows",
        sa.Column("id", sa.Integer(), primary_key=True),
        *_provenance(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.Column(
            "layout",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_archived(),
        *_times(),
        schema=SCHEMA,
        comment="Workflows: each a name, its current version and its layout",
    )
    op.create_index("uq_workflows_name", "workflows", ["name"], unique=True, schema=SCHEMA)
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_id", sa.Integer(), sa.ForeignKey(f"{SCHEMA}.workflows.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        *_times(updated=False),
        sa.UniqueConstraint(
            "workflow_id", "version", name="uq_workflow_versions_workflow_id_version"
        ),
        schema=SCHEMA,
        comment="Each workflow's versions, which never change once saved",
    )
    op.create_foreign_key(
        "fk_workflows_current_version_id",
        "workflows",
        "workflow_versions",
        ["current_version_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_table(
        "workflow_nodes",
        sa.Column(
            "workflow_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.workflow_versions.id"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column(
            "kind",
            sa.Enum("agent", "workflow", name="workflow_node_kind", schema=SCHEMA),
            nullable=False,
        ),
        sa.Column(
            "agent_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_versions.id"),
            nullable=True,
        ),
        sa.Column(
            "subworkflow_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.workflow_versions.id"),
            nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(kind = 'agent' AND agent_version_id IS NOT NULL AND subworkflow_version_id IS NULL)"
            " OR (kind = 'workflow' AND subworkflow_version_id IS NOT NULL"
            " AND agent_version_id IS NULL)",
            name="ck_workflow_nodes_target",
        ),
        schema=SCHEMA,
        comment="Each workflow version's nodes: an agent version or a workflow version",
    )
    op.create_index(
        "ix_workflow_nodes_agent_version_id", "workflow_nodes", ["agent_version_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_workflow_nodes_subworkflow_version_id",
        "workflow_nodes",
        ["subworkflow_version_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "workflow_edges",
        sa.Column("workflow_version_id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("target", sa.String(64), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "source"],
            [f"{SCHEMA}.workflow_nodes.workflow_version_id", f"{SCHEMA}.workflow_nodes.key"],
            name="fk_workflow_edges_source",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "target"],
            [f"{SCHEMA}.workflow_nodes.workflow_version_id", f"{SCHEMA}.workflow_nodes.key"],
            name="fk_workflow_edges_target",
        ),
        sa.ForeignKeyConstraint(["workflow_version_id"], [f"{SCHEMA}.workflow_versions.id"]),
        schema=SCHEMA,
        comment="Each workflow version's edges: control passes from source to target",
    )


def downgrade() -> None:
    op.drop_constraint("fk_workflows_current_version_id", "workflows", schema=SCHEMA)
    op.drop_constraint("fk_agents_current_version_id", "agents", schema=SCHEMA)
    op.drop_constraint("fk_skills_current_version_id", "skills", schema=SCHEMA)
    for table in (
        "workflow_edges",
        "workflow_nodes",
        "workflow_versions",
        "workflows",
        "agent_version_tools",
        "agent_version_skills",
        "agent_versions",
        "agents",
        "skill_versions",
        "skills",
    ):
        op.drop_table(table, schema=SCHEMA)
    op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.workflow_node_kind")
