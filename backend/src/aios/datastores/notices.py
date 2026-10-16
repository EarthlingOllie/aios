"""What agents' tools tell the model about a data store they can't use yet."""

from aios.models import DataStoreStatus


def store_not_ready(status: DataStoreStatus | None) -> str:
    """Why the user's store can't be read, given its status (`None`: there's no such store).
    Worded like the app's own notice (`aios.main`), so the model tells the user what they see."""
    if status == DataStoreStatus.provisioning:
        return "The user's data store is still being set up, so there's nothing in it yet."
    if status == DataStoreStatus.failed:
        return (
            "The user's data store couldn't be set up. It will be tried again automatically;"
            " the user can check back later."
        )
    return "The user's data store isn't available."
