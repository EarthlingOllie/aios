"""What agents' tools tell the model about a data store they can't use (yet, or just now)."""

from aios.models import DataStoreStatus


def store_not_ready(status: DataStoreStatus | None) -> str:
    """Why the organisation's store can't be read, given its status (`None`: there's no such store).
    Worded like the app's own notice (`aios.main`), so the model tells the user what they see."""
    if status == DataStoreStatus.provisioning:
        return "The organisation's data store is still being set up, so there's nothing in it yet."
    if status == DataStoreStatus.failed:
        return (
            "The organisation's data store couldn't be set up. It will be tried again"
            " automatically; the user can check back later."
        )
    return "The organisation's data store isn't available."


def store_upgrading(retry: str) -> str:
    """What to tell the model while the store is brought to the store tree's head
    (`DataStoreBehind`): worth trying `retry` (e.g. "the search") again in a moment."""
    return f"The organisation's data store is being upgraded. Try {retry} again in a moment."


def store_busy(retry: str) -> str:
    """What to tell the model when the store has no connections free (`DataStoreBusy`): worth
    trying `retry` again in a moment."""
    return f"The organisation's data store is busy. Try {retry} again in a moment."
