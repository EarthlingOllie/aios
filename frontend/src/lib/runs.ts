/**
 * How a workflow run went, node by node: what the run page draws on the graph.
 *
 * A step exists only once its node has started (`plan/workflow-editor/runs-api.md`), so a node
 * without one is waiting while the run is active, and never ran once it has ended.
 */
import type { WorkflowEdge, WorkflowRunStatus, WorkflowRunStep } from "@/lib/api";

export type NodeState = WorkflowRunStep["status"] | "waiting" | "not-run";
/** taken: its source's output went along it. live: and its target is running now. */
export type EdgeState = "taken" | "live" | "idle";

export const isActive = (status: WorkflowRunStatus) => status === "queued" || status === "running";

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
  return Object.fromEntries(
    keys.map((key) => [
      key,
      stepAt(steps, [...prefix, key])?.status ?? (active ? "waiting" : "not-run"),
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
      const state: EdgeState =
        source?.status !== "succeeded" ? "idle" : target?.status === "running" ? "live" : "taken";
      return [`${e.source}->${e.target}`, state];
    }),
  );
}
