"""notification kinds and references

Notifications hold references, never content, and at most one is unread per person,
organisation, kind and reference (`ux_notifications_unread`).

- A schema change request's (`schema_change_request`, with `request_id` and `data_store_id`)
  becomes `approval_needed`, naming the approval: `approval_id`. The store is the
  organisation's, which the notification already names.
- An organisation's deletion, scheduled or cancelled, no longer keeps its name and date: it's
  about its organisation (`{}`), whose name is read when it's listed.
- A released document that changed keeps only `document_id`, not its filename or store.

Unread repeats about the same thing are marked read, keeping the newest. The downgrade puts the
old kind and keys back (the store's id from the organisation's) and drops the index; names and
filenames, gone, are left out.

Revision ID: 0074
Revises: 0073
Create Date: 2026-11-20 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE notifications
        SET kind = 'approval_needed',
            ref = jsonb_build_object('approval_id', ref->>'request_id')
        WHERE kind = 'schema_change_request'
        """
    )
    op.execute(
        """
        UPDATE notifications SET ref = '{}'::jsonb
        WHERE kind IN ('organisation_deletion_scheduled', 'organisation_deletion_cancelled')
        """
    )
    op.execute(
        """
        UPDATE notifications SET ref = jsonb_build_object('document_id', ref->'document_id')
        WHERE kind = 'released_document_changed'
        """
    )
    op.execute(
        """
        UPDATE notifications n SET read_at = now()
        WHERE n.read_at IS NULL AND EXISTS (
            SELECT 1 FROM notifications m
            WHERE m.read_at IS NULL
              AND m.user_id = n.user_id
              AND m.organisation_id = n.organisation_id
              AND m.kind = n.kind
              AND m.ref = n.ref
              AND (m.created_at, m.id::text) > (n.created_at, n.id::text)
        )
        """
    )
    op.create_index(
        "ux_notifications_unread",
        "notifications",
        ["user_id", "organisation_id", "kind", "ref"],
        unique=True,
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_notifications_unread", table_name="notifications")
    op.execute(
        """
        UPDATE notifications n
        SET kind = 'schema_change_request',
            ref = jsonb_build_object(
                'request_id', n.ref->>'approval_id', 'data_store_id', s.id::text
            )
        FROM data_stores s
        WHERE n.kind = 'approval_needed' AND s.owner_organisation_id = n.organisation_id
        """
    )
    op.execute(
        "DELETE FROM notifications WHERE kind IN ('approval_needed', 'mention', 'reply',"
        " 'run_failed')"
    )
