"""What agent runs' statuses can be.

Agents and skills, with their versions, and agent runs with their sandbox sessions, are in each
organisation's store (`aios.datastores.models`).
"""

import enum


class AgentRunStatus(enum.StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
