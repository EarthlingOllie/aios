"""Workflows: graphs of agents, and of other workflows run as subgraphs.

A workflow version is a directed acyclic graph. Each node pins an agent version or a workflow
version; an edge says control passes from one node to the next when it finishes. Nodes with no
incoming edge are where the workflow starts, and it ends when every node has run.

A run goes node by node on the run service (`advance`), holding no process while it waits on
the model or on a decision. Each advance rebuilds the run's state from its recorded steps and
starts the nodes now ready, so independent branches run at the same time. A node's input is its
predecessors' outputs, in edge order, joined with a blank line; start nodes get the run's input.
An agent node runs its agent version as a new agent run (one turn, `aios.runner`), as a child
invocation of the run's, and outputs the agent's final reply; that agent run is started by
whoever started the workflow run. A run executes for the workflow's organisation, whoever
started it (a member, a chat's mention, a schedule): its agents' tools use the organisation's
store, and their traces carry the organisation and the starter. A workflow node runs its
version's graph within its parent's advances, with the node's input, and outputs its sink nodes'
outputs (those with no outgoing edge), in node order. Each node records a step
(`workflow_run_steps`) when it starts and finishes, so the run can be watched as it goes. Once a
node fails, no more nodes start; those already running finish, and the run fails with the first
error.

An agent node whose agent asks for a schema change (`request_schema_change`, a LangGraph
interrupt) waits for a member's decision: its step becomes `waiting`, with the request, and the
nodes that don't depend on it carry on. Once nothing else can run, the run becomes `waiting`. The
decision enqueues an advance, which resumes the waiting node on its agent run's thread with the
outcome. It's still on the versions it pinned; a pin that can no longer run fails its step when
it runs again. Waiting time isn't running time: the run's time limit counts only the latter.

Split into `validation` (the graph), `steps` (its steps, and an agent node's turn), `advance`
(moving a run on) and `execution` (ending it). Every name is re-exported here, where callers
import it; `_organisation`, `trace_metadata` and `_finish_step` are read here at call time, so a
test that replaces one here replaces it for every part.

The workflow's graph (its versions, nodes and edges, and the agent versions they pin), the run,
its steps and its agent nodes' runs are all in the organisation's store, and a run is named by
its store and its id there (ids repeat across stores): its run service items carry both. What
the platform keeps of a run (the notifications of the schema change requests its nodes made) is
written once the store's change has committed (`_record_ended`).
"""

from aios.workflows.execution import (
    NOT_ENDED,
    Ended,
    _end_runs,
    _organisation,
    _record_ended,
    alive,
    close_requests,
    fail_run,
    finish_run,
    time_limit_error,
    workflow_run_waiting_on,
)
from aios.workflows.steps import (
    NodeWaiting,
    RunState,
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
    "Ended",
    "Graph",
    "GraphError",
    "KEY_PATTERN",
    "NOT_ENDED",
    "Node",
    "NodeWaiting",
    "RunError",
    "RunState",
    "_end_runs",
    "_finish_step",
    "_organisation",
    "_record_ended",
    "_start_step",
    "advance_run",
    "advance_work",
    "alive",
    "archived_in_graph",
    "close_requests",
    "fail_run",
    "finish_run",
    "join_outputs",
    "load_graph",
    "node_work",
    "prefetch_graph",
    "reachable_versions",
    "run_node",
    "time_limit_error",
    "trace_metadata",
    "validate_graph",
    "workflow_run_waiting_on",
]

# After the names it reads on the package.
from aios.workflows.advance import advance_run, advance_work, node_work, run_node  # noqa: E402
