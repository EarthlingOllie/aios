"""What agents' schema change requests can be.

The requests are in each organisation's store (`aios.datastores.models.SchemaChangeRequest`);
the platform's notifications name them by id and store.
"""

import enum


class SchemaChangeStatus(enum.StrEnum):
    pending = "pending"  # waiting for a member to decide it
    approved = "approved"  # being applied
    rejected = "rejected"
    expired = "expired"  # undecided for too long; counts as rejected
    applied = "applied"
    failed = "failed"  # applying it failed; the schema is unchanged
    stale = "stale"  # the schema changed since, and it no longer applies
    cancelled = "cancelled"  # the run that asked was deleted, or can't resume

    @property
    def decided(self) -> bool:
        """Whether this is an outcome the run that asked can be resumed with."""
        return self not in (SchemaChangeStatus.pending, SchemaChangeStatus.approved)
