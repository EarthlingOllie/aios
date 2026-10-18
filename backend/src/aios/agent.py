import uuid
from dataclasses import dataclass


@dataclass
class ChatDeps:
    """The runtime context of an agent run (LangGraph `context_schema`); tools read it."""

    # The user the run is for: its agent's owner (`aios.runner`).
    user_id: int
    # The agent run, e.g. for the run's sandbox (`run_python`).
    run_id: int | None = None
    # The data store the run's tools read and write (`aios.datastores`), resolved when the run's
    # turn starts. Tools take it rather than the user, so they needn't change when a
    # run's store isn't its user's (teams).
    data_store_id: uuid.UUID | None = None
