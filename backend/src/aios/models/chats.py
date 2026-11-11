"""What chats, their members and messages, and invocations can be.

Chats, chat members, messages and invocations are in each organisation's store
(`aios.datastores.models`).
"""

import enum


class ChatMemberKind(enum.StrEnum):
    user = "user"
    agent = "agent"
    workflow = "workflow"


class ChatAuthorKind(enum.StrEnum):
    user = "user"
    agent = "agent"
    workflow = "workflow"
    # The chat itself (no author), for messages about it.
    system = "system"


class ChatMessageKind(enum.StrEnum):
    # Markdown, as written.
    text = "text"
    # An acknowledgement from the agent or workflow a mention started, with a card under it
    # following the run (`invocation_id`).
    run_card = "run_card"


class InvocationCause(enum.StrEnum):
    """Why a run ran. Only mentions make invocations so far; schedules and runs started by hand
    will (`aios.invocations`)."""

    mention = "mention"
    schedule = "schedule"
    manual = "manual"


class InvocationStatus(enum.StrEnum):
    # Waiting for its run to start (`aios.invocations`).
    queued = "queued"
    # The handoff is briefing its run on the chat (`aios.handoff`); the run starts next.
    briefing = "briefing"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    # A mention the poster may not make (the target's invocable-by, `aios.guards`): posted with
    # its card saying why, and nothing ever ran.
    refused = "refused"

    @property
    def finished(self) -> bool:
        return self in (
            InvocationStatus.succeeded,
            InvocationStatus.failed,
            InvocationStatus.refused,
        )
