"""Sources: what brings content into an organisation's store.

Every store has exactly one upload source, Uploads, made with the store (the store tree's
`0011`): everything members upload belongs to it, so "which source" is the same question for
every document. It can't be paused, changed or removed (`UPLOADS_FIXED`). Later kinds (URL,
webhook, email, connector) sync content in; their rows are in the same table.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from aios.datastores.models import Source, SourceKind

# Why Uploads can't be paused, changed or removed: what the API answers with its 409.
UPLOADS_FIXED = (
    "Uploads can't be paused or removed: it's where every uploaded document goes, so uploading "
    "is always on."
)


def uploads_source_id(store: Session) -> int:
    """The id of the store's Uploads source, which uploads belong to."""
    return store.scalars(select(Source.id).where(Source.kind == SourceKind.upload)).one()
