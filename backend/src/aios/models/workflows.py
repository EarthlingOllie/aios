"""What workflow nodes, runs and steps can be.

Workflows, with their versions, nodes and edges, and workflow runs with their steps, are in each
organisation's store (`aios.datastores.models`).
"""

import enum


class WorkflowNodeKind(enum.StrEnum):
    agent = "agent"
    workflow = "workflow"
    # Waits for a person in its role to approve or reject it (`aios.approvals`).
    approval = "approval"


# A workflow run walks its pinned version's graph, recording a step per node as it starts. An
# agent node's conversation is an ordinary agent run, so it opens on the agent's run page.


class WorkflowRunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    # Paused on a node waiting for an approval (a schema change its agent asked for, or an
    # approval node's), holding no worker; queued again once it's decided (`aios.workflows`).
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"


class WorkflowStepStatus(enum.StrEnum):
    running = "running"
    # An agent node waiting for a schema change request, an approval node waiting for its
    # decision, or a workflow node one of whose nodes is waiting.
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    # No active path reached it: none of its incoming edges is active (its sources were
    # skipped, or their conditions were false). Recorded with no agent run; its successors
    # may be skipped in turn (`aios.workflows.advance`).
    skipped = "skipped"
