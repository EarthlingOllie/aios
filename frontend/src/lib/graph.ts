import { graphlib, layout } from "@dagrejs/dagre";

/** Where a node is drawn: its top-left corner, in graph coordinates. */
export type Position = { x: number; y: number };

export type GraphEdge = { source: string; target: string };

// Every node is drawn at the same size, so layouts don't depend on rendering.
export const NODE_WIDTH = 200;
export const NODE_HEIGHT = 64;

/** Nodes with no incoming edge, in node order: where a run starts. */
export function startKeys(keys: string[], edges: GraphEdge[]): string[] {
  const targets = new Set(edges.map((e) => e.target));
  return keys.filter((key) => !targets.has(key));
}

/** Nodes with no outgoing edge, in node order: their outputs are the workflow's output. */
export function endKeys(keys: string[], edges: GraphEdge[]): string[] {
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
  edges: GraphEdge[],
  saved: Record<string, Position>,
): Record<string, Position> {
  if (keys.length === 0) return {};
  const g = new graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 32, ranksep: 64 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const key of keys) g.setNode(key, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const { source, target } of edges) {
    if (g.hasNode(source) && g.hasNode(target)) g.setEdge(source, target);
  }
  layout(g);
  // dagre gives centres; positions are top-left corners.
  const auto = (key: string): Position => {
    const { x, y } = g.node(key);
    return { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 };
  };

  const shift: Record<string, Position> = {};
  for (const key of keys) {
    if (saved[key]) {
      const a = auto(key);
      shift[key] = { x: saved[key].x - a.x, y: saved[key].y - a.y };
    }
  }
  const savedShifts = Object.values(shift);
  const fallback =
    savedShifts.length === 0
      ? { x: 0, y: 0 }
      : {
          x: savedShifts.reduce((sum, s) => sum + s.x, 0) / savedShifts.length,
          y: savedShifts.reduce((sum, s) => sum + s.y, 0) / savedShifts.length,
        };
  // Unplaced nodes take a placed neighbour's shift. Repeated until nothing changes, so a chain of
  // new nodes follows the placed node it hangs off, whatever order the keys are in.
  let pending = keys.filter((key) => !(key in shift));
  while (pending.length) {
    const before = pending.length;
    pending = pending.filter((key) => {
      const neighbour =
        edges.find((e) => e.target === key && e.source in shift)?.source ??
        edges.find((e) => e.source === key && e.target in shift)?.target;
      if (neighbour === undefined) return true;
      shift[key] = shift[neighbour];
      return false;
    });
    if (pending.length === before) {
      for (const key of pending) shift[key] = fallback;
      break;
    }
  }

  return Object.fromEntries(
    keys.map((key) => {
      const a = auto(key);
      return [key, { x: a.x + shift[key].x, y: a.y + shift[key].y }];
    }),
  );
}
