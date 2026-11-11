"""Workflows: graphs of agents, and of other workflows run as subgraphs.

A workflow version is a directed acyclic graph. Each node pins an agent version or a workflow
version; an edge says control passes from one node to the next when it finishes. Nodes with no
incoming edge are where the workflow starts, and it ends when every node has run.

Running one (`execute_run`, in the worker) walks its pinned version's graph. A node starts once
all its predecessors have succeeded, so independent branches run at the same time. Its input is
their outputs, in edge order, joined with a blank line; start nodes get the run's input. An agent
node runs its agent version as a new agent run (one turn, `aios.runner`) and outputs the agent's
final reply; that agent run is started by whoever started the workflow run. A run executes for
the workflow's organisation, whoever started it (a member, a chat's mention, a schedule): its
agents' tools use the organisation's store, and their traces carry the organisation and the
starter. A workflow node runs its version's graph the same way, with the node's input, and
outputs its sink nodes' outputs (those with no outgoing edge), in node order. Each node records a
step (`workflow_run_steps`) when it starts and finishes, in its own transaction, so the run can be
watched as it goes. Once a node fails, no more nodes start; those already running finish, and the
run fails with the first error.

An agent node whose agent asks for a schema change (`request_schema_change`, a LangGraph
interrupt) waits for a member's decision: its step becomes `waiting`, with the request, and the
nodes that don't depend on it carry on. Once nothing else can run, the run becomes `waiting` and
its task returns (`_pause_run`), holding no worker. The decision queues it again
(`aios.worker.enqueue_resume`): claimed from `waiting`, it rebuilds its state from the steps it
recorded, and the waiting node resumes on its agent run's thread with the outcome. It's still on
the versions it pinned; a pin that can no longer run fails its step when it runs again. Waiting
time isn't running time: the run's time limit counts only the latter (`Claimed.time_left`).

While a worker runs a run, it refreshes the run's heartbeat (`heartbeat_at`) every
`HEARTBEAT_SECONDS`, from a thread of its own, so a node that holds the event loop doesn't stop
it (`_Heartbeat`). A run is alive while it's running with a recent heartbeat (`alive`); one
running without, whose worker died (killed, OOM, restarted), is failed when its task comes round
again or when it's read (`fail_stopped_runs`), and can be deleted. The same thread fails a run
whose worker's event loop has made no progress for `workflow_run_no_progress_limit` (stuck in a
blocking call, a deadlock), and stops it if the loop frees up; a node awaiting a slow call keeps
the loop free, so it isn't failed.

Split into `validation` (the graph), `steps` (running its nodes) and `execution` (the run). Every
name is re-exported here, where callers import it; `_organisation`, `trace_metadata`,
`_start_step`, `_finish_step`, `_beat`, `HEARTBEAT_SECONDS` and `STALE_AFTER` are read here at
call time, so a test that replaces one here replaces it for every part.

The workflow's graph (its versions, nodes and edges, and the agent versions they pin), the run,
its steps and its agent nodes' runs are all in the organisation's store, and a run is named by
its store and its id there (ids repeat across stores): its task carries both. What the platform
keeps of a run (the notifications of the schema change requests its nodes made) is written once
the store's change has committed (`_record_ended`).
"""

from aios.workflows.execution import (
    HEARTBEAT_SECONDS,
    NOT_ENDED,
    STALE_AFTER,
    WORKER_STOPPED,
    Claimed,
    Ended,
    _beat,
    _claim,
    _end_runs,
    _organisation,
    _record_ended,
    alive,
    close_requests,
    execute_run,
    fail_run,
    fail_stopped_runs,
    no_progress_error,
    time_limit_error,
    workflow_run_waiting_on,
)
from aios.workflows.steps import (
    NodeWaiting,
    RecordedStep,
    RunState,
    StepFailed,
    _finish_step,
    _start_step,
    join_outputs,
    trace_metadata,
)
from aios.workflows.validation import (
    KEY_PATTERN,
    AgentNode,
    Graph,
    GraphError,
    Node,
    RunError,
    archived_in_graph,
    load_graph,
    prefetch_graph,
    reachable_versions,
    validate_graph,
)

__all__ = [
    "AgentNode",
    "Claimed",
    "Ended",
    "Graph",
    "GraphError",
    "HEARTBEAT_SECONDS",
    "KEY_PATTERN",
    "NOT_ENDED",
    "Node",
    "NodeWaiting",
    "RecordedStep",
    "RunError",
    "archived_in_graph",
    "RunState",
    "STALE_AFTER",
    "StepFailed",
    "WORKER_STOPPED",
    "_beat",
    "_claim",
    "_end_runs",
    "_organisation",
    "_record_ended",
    "_finish_step",
    "_start_step",
    "alive",
    "close_requests",
    "execute_run",
    "fail_run",
    "fail_stopped_runs",
    "join_outputs",
    "load_graph",
    "no_progress_error",
    "prefetch_graph",
    "reachable_versions",
    "time_limit_error",
    "trace_metadata",
    "validate_graph",
    "workflow_run_waiting_on",
]
