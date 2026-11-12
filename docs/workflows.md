# Workflows

**Workflows** graph agents together, and can run other workflows as subgraphs
(`/api/o/{org}/workflows`). Like agents, they belong to the organisation (see
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

Workflows are versioned like agents (`workflows`, `workflow_versions`, in the organisation's
store with its agents; see [Agents and skills](agents-and-skills.md#versions) and
[Stores](stores.md#agents-skills-and-workflows)):

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
  versions, and stay readable, an archived agent or workflow still showing its latest version.
  A new version can't pin an archived agent or workflow ("Unknown agent version"), and restoring
  one that runs something since archived is refused. Names are unique among all the
  organisation's rows, archived ones included, so an archived item's name stays reserved.
  Archiving an agent is covered in
  [Agents and skills](agents-and-skills.md#archiving-an-agent).

## Archiving a workflow

A workflow is archived on the same terms as an agent: from its page, in one confirmation that's
never blocked, by any member.

- `POST /api/o/{org}/workflows/{id}/archive` sets `archived_at` and `archived_by` and pauses its
  schedules; `/unarchive` clears both and leaves the schedules paused. Both answer the workflow,
  which carries its archived fields everywhere.
- `GET /api/o/{org}/workflows/{id}/used-by` is what the confirmation shows, read-only
  (`aios.used_by.workflow`): the live workflows whose current version nests it at any depth
  (with their schedules), its own schedules, and the chats it's in.
- It starts nothing new. Each start path refuses with 409 "The workflow <name> is archived.": a
  run by hand, a schedule firing (recorded on the schedule), and a run of a live workflow that
  nests it at any depth (`aios.workflows.archived_in_graph`). A mention posts the message, and
  its invocation fails at once with "The workflow <name> is archived", before the handoff.
- Runs already going finish, and its runs, versions and page stay readable.
- In a chat it stays a member, shown as archived, and drops out of mention suggestions.
- `GET /api/o/{org}/workflows` leaves archived workflows out; `?archived=true` lists only them,
  which the Workflows list's Archived filter shows. Its page shows an archived badge and notice,
  and Unarchive in place of Run. Its graph can still be edited.

## Runs

- A **run** (`workflow_runs`, in the organisation's store with its steps and its agent nodes'
  runs: see [Stores](stores.md#runs)) is given one text input and pins the version that's
  current when it starts. It goes node by node on the [run service](run-service.md), holding no
  process while it waits on the model or on a decision (`aios.workflows.advance`):
  - a **workflow advance** item, under the run's row lock, rebuilds the run's state from its
    recorded steps, checks its time limit, records what has finished, starts the steps of the
    nodes now ready and enqueues their items, and ends the run once nothing is ready and nothing
    is running. One is queued per run at a time; one enqueued while another runs makes that one
    run again once it's done, so no step finishing meanwhile is missed. Subworkflow nodes are
    expanded within it;
  - a **workflow node** item runs an agent node's turn, as a child invocation of the run's, and
    enqueues an advance when it's done. Retried after its process died, it carries on the turn
    from its last checkpoint rather than giving the agent its message again; one whose attempts
    run out fails its step, and the run, with "The service stopped mid-run."
  - The run's time limit, `WORKFLOW_RUN_TIME_LIMIT` (2 hours), counts active time only: time
    spent waiting on a decision doesn't count (`waited_seconds`).
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
- An approval node waits for a person's decision (see [Approval nodes](#approval-nodes)).
- Starting a run is refused if any agent version in the graph, at any depth, couldn't run (a
  tool that has left the library), if an agent or workflow in it is archived ("The agent <name>
  is archived."), or if it has no nodes. A run already going isn't checked again: an agent
  archived since it started still runs its node. Deleting a run deletes its agent runs;
  archiving the workflow leaves its runs.

## Approval nodes

An **approval node** (kind `approval`, pinning nothing) pauses a run for a person in a set role
to approve or reject it (`aios.approvals`; one of the two kinds of approval, beside schema
changes, in the Data page's Approvals list).

- **Configured** with a prompt template (placeholders as an input template's: `{{input}}`,
  `{{nodes.<key>}}` and fields of them, predecessors only), the approver role (`editor`, the
  default, or `admin`: that role or above decides it) and an optional expiry in days (else the
  organisation's approval expiry setting). It declares no output schema: its output is its
  decision. Saving refuses a missing prompt, a placeholder naming a non-predecessor, or these
  fields on another kind of node, each with the item it's about (422).
- **Reached**, it renders its prompt and records a pending workflow step approval with the
  prompt (its reason, in full) and its input; its step waits on it (`approval_id`), and the run
  waits once nothing else can run. Only members whose role may decide it are notified ("approval
  needed").
- **Decided** (approved or rejected, with an optional note, in the Approvals list or on the run's
  page), expired, or cancelled, the run resumes and the node outputs
  `{approved, note, decided_by: {id, name}, status}` (`status` approved, rejected, expired or
  cancelled; an expired approval counts as not approved), which its outgoing edges' conditions
  branch on, e.g. `{"==": [{"var": "output.approved"}, true]}`.
- **Cancelling the run** (`POST /api/o/{org}/workflows/{id}/runs/{run_id}/cancel`, anyone who may
  start runs) fails it ("Cancelled by <name>."), cancels the approvals it waits on and clears
  their notifications; deleting it does the same.

