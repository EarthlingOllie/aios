# Workflow runs: API contract

The contract between the runs backend (PR 2 of the workflow editor stack) and the run
inspection UI (PR 5). Both sides build against this document; change it here first.

## Semantics

- A run pins the workflow's current version when it starts, like an agent run.
- A run is given one text `input`. Nodes with no incoming edge (start nodes) receive it.
- A node runs once all its predecessors have succeeded. Its input is its predecessors'
  outputs, in edge order, joined with a blank line (a start node gets the run's input).
- An **agent node** runs its pinned agent version as a fresh agent run (`agent_runs` row, so
  the conversation can be opened on the existing run page). Its input is the user message,
  and its output is the text of the agent's final reply.
- A **workflow node** runs its pinned workflow version as a subgraph with the node's input. Its
  output is the outputs of the subgraph's sink nodes (no outgoing edge), in node order,
  joined with a blank line. Its own steps are recorded too, with a longer `path`.
- If a node fails, nothing downstream of it runs (those steps are never created). Branches
  already running are allowed to finish. The run then fails with the first error.
- The run's output is the outputs of the top-level sink nodes, joined like a subgraph's.
- A run whose worker stopped mid-run (crash, restart) is reported `failed`, within a couple of
  minutes, with the error "The worker stopped while running this, so it couldn't finish."; its
  steps still running are failed too.

## Statuses

- run: `queued` → `running` → `succeeded` | `failed`
- step: `running` → `succeeded` | `failed`

Steps exist only once a node has started, so a node with no step hasn't run (yet).

## Endpoints

All are scoped to the signed-in user's live workflow (404 otherwise).

`POST /api/workflows/{id}/runs` with `{"input": "..."}` → 201 `WorkflowRunSummary`.
Starts the run in the background. 409 if the current version can't run (e.g. an agent
version whose tools left the library). 422 if the workflow has no nodes.

`GET /api/workflows/{id}/runs` → `WorkflowRunSummary[]`, newest first.

`GET /api/workflows/{id}/runs/{run_id}` → `WorkflowRunOut`. Polled by the UI every ~1s while
the run's status is `queued` or `running`.

`DELETE /api/workflows/{id}/runs/{run_id}` → 204. Also deletes the agent runs its steps created.
409 while the run is `running` (a queued one can be deleted, and so can one whose worker
stopped), so the UI should disable delete until it finishes.

```ts
type WorkflowRunStatus = "queued" | "running" | "succeeded" | "failed";
type WorkflowStepStatus = "running" | "succeeded" | "failed";

type WorkflowRunSummary = {
  id: number;
  workflow_id: number;
  workflow_version_id: number;
  workflow_version: number;      // the pinned version's number
  status: WorkflowRunStatus;
  input: string;
  output: string | null;         // set when succeeded
  error: string | null;          // set when failed
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

type WorkflowRunStep = {
  id: number;
  // Node keys from the run's workflow down to this node: ["research"] for a top-level node,
  // ["review", "critic"] for node "critic" inside the subworkflow at top-level node "review".
  path: string[];
  kind: "agent" | "workflow";
  status: WorkflowStepStatus;
  input: string;
  output: string | null;
  error: string | null;
  // Agent nodes: the agent run holding the conversation (open at
  // /agents/{agent_id}/runs/{agent_run_id}); null for workflow nodes.
  agent_id: number | null;
  agent_run_id: number | null;
  started_at: string;
  finished_at: string | null;
};

type WorkflowRunOut = WorkflowRunSummary & {
  // The pinned version's graph, as GET /api/workflows/{id}/versions/{n} returns it.
  version: WorkflowVersionOut;
  steps: WorkflowRunStep[];      // in start order
};
```
