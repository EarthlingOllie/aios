import uuid
from dataclasses import dataclass


@dataclass
class ChatDeps:
    """The runtime context of an agent run (LangGraph `context_schema`); tools read it."""

    # The member who started the run; None for a schedule's (or once that user is deleted). The
    # run executes for its agent's organisation either way: tools act on its store, not the user.
    user_id: int | None
    # The agent run, e.g. for the run's sandbox (`run_python`).
    run_id: int | None = None
    # The data store the run's tools read and write (`aios.datastores`), resolved when the run's
    # turn starts: the organisation's.
    data_store_id: uuid.UUID | None = None
