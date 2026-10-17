/**
 * How a workflow run went, node by node: what the run page draws on the graph.
 *
 * A step exists only once its node has started (`plan/workflow-editor/runs-api.md`), so a node
 * without one is pending while the run is active (waiting included), and never ran once it has
 * ended, or once any node of the run has failed (no more nodes start then, though those running
 * finish). A node with a waiting step waits for a schema change request to be decided.
 */
import type { WorkflowEdge, WorkflowRunStatus, WorkflowRunStep } from "@/lib/api";
import { edgeId } from "@/lib/graph";

export type NodeState = WorkflowRunStep["status"] | "pending" | "not-run";
/** taken: its source succeeded and its target started. live: and the target is running now. */
export type EdgeState = "taken" | "live" | "idle";

/** How often a waiting run is read again (ms): it can wait for days, so less often. */
export const WAITING_POLL_MS = 5000;

/** Whether the run hasn't ended: it may still start nodes. */
export const isActive = (status: WorkflowRunStatus) =>
  status === "queued" || status === "running" || status === "waiting";

const samePath = (a: string[], b: string[]) =>
  a.length === b.length && a.every((key, i) => key === b[i]);

/** The step of the node at `path` (node keys from the run's workflow down), if it started. */
export function stepAt(steps: WorkflowRunStep[], path: string[]) {
  return steps.find((s) => samePath(s.path, path));
}

/** Each node's state, for the graph at `prefix` (the path of the workflow node it's inside). */
export function nodeStates(
  keys: string[],
  steps: WorkflowRunStep[],
  prefix: string[],
  active: boolean,
): Record<string, NodeState> {
  const starting = active && !steps.some((s) => s.status === "failed");
  return Object.fromEntries(
    keys.map((key) => [
      key,
      stepAt(steps, [...prefix, key])?.status ?? (starting ? "pending" : "not-run"),
    ]),
  );
}

/** Each edge's state, by edge id (`source->target`), for the graph at `prefix`. */
export function edgeStates(
  edges: WorkflowEdge[],
  steps: WorkflowRunStep[],
  prefix: string[],
): Record<string, EdgeState> {
  return Object.fromEntries(
    edges.map((e) => {
      const source = stepAt(steps, [...prefix, e.source]);
      const target = stepAt(steps, [...prefix, e.target]);
      // Taken only once the target has started: a succeeded source alone doesn't mean its output
      // went anywhere (the target may be waiting on, or blocked by, another predecessor).
      const state: EdgeState =
        source?.status !== "succeeded" || !target
          ? "idle"
          : target.status === "running"
            ? "live"
            : "taken";
      return [edgeId(e), state];
    }),
  );
}
