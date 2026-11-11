"""Workflow runs and their steps."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Double,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base

# Workflows themselves, with their versions, nodes and edges, are in each organisation's store
# (`aios.datastores.models`). Runs stay here for now, naming the workflow and version they ran by
# id within their organisation's store.


class WorkflowNodeKind(enum.StrEnum):
    agent = "agent"
    workflow = "workflow"


# A workflow run walks its pinned version's graph, recording a step per node as it starts. An
# agent node's conversation is an ordinary agent run, so it opens on the agent's run page.


class WorkflowRunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    # Paused on a node waiting for a schema change request, holding no worker; queued again
    # once it's decided (`aios.workflows`).
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"


class WorkflowStepStatus(enum.StrEnum):
    running = "running"
    # An agent node waiting for a schema change request, or a workflow node one of whose nodes
    # is waiting.
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"


class WorkflowRun(Base):
    """One run of a workflow, on the version that was current when it started
    (`aios.workflows.execute_run`)."""

    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The organisation whose workflow it runs (`aios.organisations`): its store holds the
    # workflow and the version, and it goes with the organisation.
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # The workflow, in the organisation's store (`aios.datastores.models.Workflow`): ids repeat
    # across organisations, so it's named with `organisation_id`, and isn't a foreign key.
    workflow_id: Mapped[int] = mapped_column(Integer, index=True)
    # Pinned when the run starts: editing the workflow mid-run doesn't change the run.
    workflow_version_id: Mapped[int] = mapped_column(Integer, index=True)
    # The member who started it; null for a schedule's run, or once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # The trigger that started it (a schedule), if one did and it still exists.
    trigger_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "triggers.id", ondelete="SET NULL", use_alter=True, name="workflow_runs_trigger_id_fkey"
        ),
        index=True,
    )
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, name="workflow_run_status"), default=WorkflowRunStatus.queued
    )
    # Given to the nodes with no incoming edge.
    input: Mapped[str] = mapped_column(Text)
    # The sink nodes' outputs once it has succeeded; the first error once it has failed.
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Refreshed by the worker while it runs the run, so a run whose worker died can be told from
    # one still going (`aios.workflows.alive`).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # While it's waiting, since when; and how long it has waited in all before that. Waiting
    # doesn't count towards the run's time limit.
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waited_seconds: Mapped[float] = mapped_column(Double, server_default=text("0"))


class WorkflowRunStep(Base):
    """A node of a run's graph, at any depth, from when it starts."""

    __tablename__ = "workflow_run_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    # Node keys from the run's workflow down to the node: ["review", "critic"] is node "critic"
    # of the subworkflow at top-level node "review".
    path: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    kind: Mapped[WorkflowNodeKind] = mapped_column(
        Enum(WorkflowNodeKind, name="workflow_node_kind")
    )
    status: Mapped[WorkflowStepStatus] = mapped_column(
        Enum(WorkflowStepStatus, name="workflow_step_status"), default=WorkflowStepStatus.running
    )
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # An agent node's conversation. Deleting the run deletes it (`delete_workflow_run`); if the
    # agent run goes first (deleting its agent), the step stays without it.
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    # While an agent node waits: the schema change request it waits on. (By id, not through
    # its agent run, which goes if its agent is deleted meanwhile.)
    waiting_on_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schema_change_requests.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
