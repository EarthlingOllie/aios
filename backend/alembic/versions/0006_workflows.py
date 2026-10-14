"""workflows

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14 07:39:53.393869
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name"),
    )
    op.create_index(op.f("ix_workflows_owner_id"), "workflows", ["owner_id"], unique=False)
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "version"),
    )
    op.create_table(
        "workflow_nodes",
        sa.Column("workflow_version_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.Enum("agent", "workflow", name="workflow_node_kind"), nullable=False),
        sa.Column("agent_version_id", sa.Integer(), nullable=True),
        sa.Column("subworkflow_version_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(kind = 'agent' AND agent_version_id IS NOT NULL AND subworkflow_version_id IS NULL)"
            " OR (kind = 'workflow' AND subworkflow_version_id IS NOT NULL"
            " AND agent_version_id IS NULL)",
            name="ck_workflow_nodes_target",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["agent_versions.id"], deferrable=True, initially="DEFERRED"
        ),
        sa.ForeignKeyConstraint(
            ["subworkflow_version_id"],
            ["workflow_versions.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workflow_version_id", "key"),
    )
    op.create_index(
        op.f("ix_workflow_nodes_agent_version_id"),
        "workflow_nodes",
        ["agent_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_nodes_subworkflow_version_id"),
        "workflow_nodes",
        ["subworkflow_version_id"],
        unique=False,
    )
    op.create_table(
        "workflow_edges",
        sa.Column("workflow_version_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "source"],
            ["workflow_nodes.workflow_version_id", "workflow_nodes.key"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "target"],
            ["workflow_nodes.workflow_version_id", "workflow_nodes.key"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workflow_version_id", "source", "target"),
    )
    # workflows <-> workflow_versions reference each other, so this FK comes last.
    op.create_foreign_key(
        "fk_workflows_current_version_id",
        "workflows",
        "workflow_versions",
        ["current_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_workflows_current_version_id", "workflows", type_="foreignkey")
    op.drop_table("workflow_edges")
    op.drop_index(op.f("ix_workflow_nodes_subworkflow_version_id"), table_name="workflow_nodes")
    op.drop_index(op.f("ix_workflow_nodes_agent_version_id"), table_name="workflow_nodes")
    op.drop_table("workflow_nodes")
    op.drop_table("workflow_versions")
    op.drop_index(op.f("ix_workflows_owner_id"), table_name="workflows")
    op.drop_table("workflows")
    sa.Enum(name="workflow_node_kind").drop(op.get_bind())
