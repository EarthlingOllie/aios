"""What agents' tools tell the model about a data store they can't use yet."""

from aios.datastores.connections import DataStoreBehind, DataStoreBusy, DataStoreNotReady
from aios.models import DataStoreStatus

# While it's brought to the store tree's head (made by the release before, during a deploy).
STORE_UPGRADING = "The user's data store is being upgraded. Try again in a moment."
# While it has no connections free.
STORE_BUSY = "The user's data store is busy. Try again in a moment."


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


def store_unusable(exc: DataStoreNotReady | DataStoreBusy) -> str:
    """Why the user's store can't be read just now, given what opening it raised."""
    if isinstance(exc, DataStoreBehind):
        return STORE_UPGRADING
    if isinstance(exc, DataStoreBusy):
        return STORE_BUSY
    return store_not_ready(exc.status)
