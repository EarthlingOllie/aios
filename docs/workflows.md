# Workflows

**Workflows** graph agents together, and can run other workflows as subgraphs
(`/api/workflows`). Like agents, they belong to the organisation (see
[Accounts and organisations](accounts-and-organisations.md#what-the-organisation-owns)).

## The Workflows page

The Workflows page draws each one as a graph (React Flow, laid out with dagre); clicking a node
or an edge shows its configuration. Its list can be searched by name and description, as the
Agents list can, filtered to the empty workflows (no nodes) and sorted by name or most recently
updated. A workflow's page has tabs, as an agent's does: Graph, Runs and Versions (each with its
count) and Schedule, the open one kept in the URL (`/workflows/7/runs`); its title, actions and
the form to start a run stay above them, and a run's or a version's page links back to its tab.
Its editor adds nodes for your agents and workflows, connects them by dragging between nodes,
and removes, renames or re-points them, refusing edges that would make a cycle before you save.

Runs are started from the workflow's page; a run's page draws the path it took on the graph
(which nodes succeeded, failed or never ran, and the edges their outputs went along) and, while
it's going, polls to show where it is now. Clicking a node shows its step's input, output and
conversation, and a subworkflow node opens its own graph. A workflow's schedules are on its
Schedule tab (see [Triggers](triggers.md)), and a workflow can be mentioned in a chat (see
[Chats](chats.md)).

## Versions

Workflows are versioned like agents (`workflows`, `workflow_versions`; see
[Agents and skills](agents-and-skills.md#versions)):

- A version is a directed acyclic graph: nodes (`workflow_nodes`), each pinning one agent version
  or one workflow version by id, and edges (`workflow_edges`) between node keys. Nodes with no
  incoming edge are where the workflow starts.
- Changing the graph (nodes, edges, or which version a node pins) creates a new version;
  renaming, moving nodes in the editor (the workflow's `layout`, positions by node key) and
  no-op saves don't. Editing an agent doesn't change any workflow: the node reports its
  latest version, and taking it is a new workflow version. Restore works as for agents.
- A workflow can't contain itself, directly or through other workflows at any depth: none of the
  versions a new version pins, nor any they pin in turn, may belong to it. Nodes can only pin
  versions that already exist, so nesting is always finite.
- Agents and workflows are archived, never deleted: older workflow versions keep pinning their
  versions, and stay readable (a node whose workflow has been archived shows no latest version;
  an archived agent keeps showing its own). Deleting a workflow archives it, hidden as a deleted
  one was; it can't be deleted while a live workflow's current version runs it; older versions
  don't count, but restoring one that runs something since archived is refused. Names are
  unique among all the organisation's rows, archived ones included, so an archived item's name
  stays reserved. Deleting an agent that workflows
  run is covered in [Agents and skills](agents-and-skills.md#deleting-an-agent), with
  deactivating a workflow.

## Runs

- A **run** (`workflow_runs`) is given one text input and pins the version that's current when it
  starts. A worker of its own runs it (`aios.run_workflow`, a Celery task on the `workflows`
  queue, so ingests don't wait behind runs, with its own time limit, `WORKFLOW_RUN_TIME_LIMIT`),
  refreshing its heartbeat (`heartbeat_at`) as it goes; a run whose heartbeat has stopped (its
  worker died) is failed when it's next read or its task comes round again, and one whose
  worker's event loop has made no progress for `WORKFLOW_RUN_NO_PROGRESS_LIMIT` (5 minutes;
  stuck in a blocking call) is failed by the worker itself, while a node awaiting a slow model
  or tool call isn't.
- A node starts once all its predecessors have succeeded, so independent branches run at the
  same time; start nodes get the run's input, other nodes their predecessors' outputs in edge
  order, joined with a blank line. An agent node is a new agent run of its pinned version, given
  its input as the user's message, and outputs the final reply, so its conversation opens on the
  run page like any other. A workflow node runs its pinned version's graph with its input, and
  outputs its sink nodes' outputs (in node order); so does the run.
- Each node records a step (`workflow_run_steps`, with the path of node keys down to it) when it
  starts and finishes, committed as it goes, so the run can be watched while it's going. Once a
  node fails nothing else starts, and the run fails with the first error. Each agent node is
  traced like an agent run, with the workflow, its version, the run and the node's path added to
  the metadata (see [Tracing](tracing.md)).
- An agent node whose agent requests a schema change waits for the decision, and the run waits
  with it once nothing else can run (see [Tools](tools.md#request-schema-changes)).
- Starting a run is refused if any agent version in the graph, at any depth, couldn't run (a
  tool that has left the library), if an agent or workflow in it is archived ("The agent <name>
  is archived."), or if it has no nodes. A run already going isn't checked again: an agent
  archived since it started still runs its node. Deleting a run deletes its agent runs; deleting the workflow leaves its runs in the
  database.
