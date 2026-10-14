import { graphlib, layout } from "@dagrejs/dagre";

import type { NodePosition, WorkflowEdge } from "@/lib/api";

// Every node is drawn at the same size, so layouts don't depend on rendering.
export const NODE_WIDTH = 200;
export const NODE_HEIGHT = 64;

/** Nodes with no incoming edge, in node order: where a run starts. */
export function startKeys(keys: string[], edges: WorkflowEdge[]): string[] {
  const targets = new Set(edges.map((e) => e.target));
  return keys.filter((key) => !targets.has(key));
}

/** Nodes with no outgoing edge, in node order: their outputs are the workflow's output. */
export function endKeys(keys: string[], edges: WorkflowEdge[]): string[] {
  const sources = new Set(edges.map((e) => e.source));
  return keys.filter((key) => !sources.has(key));
}

/**
 * Positions for every node: its saved one if it has one, otherwise a left-to-right layered
 * layout (dagre).
 *
 * A node without a saved position keeps its place relative to its neighbours: it's moved by
 * as much as the nearest already-placed predecessor (or successor) was moved from where the
 * layout put it, so a node added after one that's been dragged aside still follows it.
 */
export function layoutGraph(
  keys: string[],
  edges: WorkflowEdge[],
  saved: Record<string, NodePosition>,
): Record<string, NodePosition> {
  if (keys.length === 0) return {};
  const g = new graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 32, ranksep: 64 });
  g.setDefaultEdgeLabel(() => ({}));
  // dagre keeps nodes in plain objects, so a key like `__proto__` would break it: it's given each
  // node's index instead.
  const ids = new Map(keys.map((key, i) => [key, String(i)]));
  for (const id of ids.values()) g.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const { source, target } of edges) {
    const [s, t] = [ids.get(source), ids.get(target)];
    if (s !== undefined && t !== undefined) g.setEdge(s, t);
  }
  layout(g);
  // dagre gives centres; positions are top-left corners.
  const auto = (key: string): NodePosition => {
    const { x, y } = g.node(ids.get(key)!);
    return { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 };
  };

  // A Map, and `Object.hasOwn` for saved positions, so keys like `constructor` aren't taken for
  // what every object inherits.
  const shift = new Map<string, NodePosition>();
  for (const key of keys) {
    if (Object.hasOwn(saved, key)) {
      const a = auto(key);
      shift.set(key, { x: saved[key].x - a.x, y: saved[key].y - a.y });
    }
  }
  const savedShifts = [...shift.values()];
  const fallback =
    savedShifts.length === 0
      ? { x: 0, y: 0 }
      : {
          x: savedShifts.reduce((sum, s) => sum + s.x, 0) / savedShifts.length,
          y: savedShifts.reduce((sum, s) => sum + s.y, 0) / savedShifts.length,
        };
  // Unplaced nodes take a placed neighbour's shift. Repeated until nothing changes, so a chain of
  // new nodes follows the placed node it hangs off, whatever order the keys are in.
  let pending = keys.filter((key) => !shift.has(key));
  while (pending.length) {
    const before = pending.length;
    pending = pending.filter((key) => {
      const neighbour =
        edges.find((e) => e.target === key && shift.has(e.source))?.source ??
        edges.find((e) => e.source === key && shift.has(e.target))?.target;
      if (neighbour === undefined) return true;
      shift.set(key, shift.get(neighbour)!);
      return false;
    });
    if (pending.length === before) {
      for (const key of pending) shift.set(key, fallback);
      break;
    }
  }

  return Object.fromEntries(
    keys.map((key) => {
      const a = auto(key);
      const s = shift.get(key)!;
      return [key, { x: a.x + s.x, y: a.y + s.y }];
    }),
  );
}
