import { graphlib, layout } from "@dagrejs/dagre";

import type { NodePosition, WorkflowEdge } from "@/lib/api";

// Every node is drawn at the same size, so layouts don't depend on rendering.
export const NODE_WIDTH = 200;
export const NODE_HEIGHT = 64;

/** An edge's id on the canvas, and its key in a run's edge states. */
export const edgeId = (e: WorkflowEdge) => `${e.source}->${e.target}`;

/** Nodes with no incoming edge, in node order: where a run starts. */
export function startKeys(keys: string[], edges: WorkflowEdge[]): string[] {
  const targets = new Set(edges.map((e) => e.target));
  return keys.filter((key) => !targets.has(key));
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

// How a graph is first shown: never so small its labels can't be read (a wider graph is panned
// instead, from where it starts), never blown up past its size, on a canvas only as tall as that
// needs, within limits.
export const MIN_ZOOM = 0.8;
/** While editing the whole graph is kept in view, to drag between any two nodes: smaller, then. */
export const MIN_EDITING_ZOOM = 0.4;
const MAX_ZOOM = 1;
const PADDING = 32;
export const MIN_CANVAS_HEIGHT = 288;
export const MAX_CANVAS_HEIGHT = 640;

/** The canvas height and viewport to first show nodes at these positions on a canvas this wide. */
export function fitGraph(
  positions: NodePosition[],
  width: number,
  minZoom = MIN_ZOOM,
): { height: number; viewport: { x: number; y: number; zoom: number } } {
  if (positions.length === 0) {
    return { height: MIN_CANVAS_HEIGHT, viewport: { x: 0, y: 0, zoom: MAX_ZOOM } };
  }
  const left = Math.min(...positions.map((p) => p.x));
  const top = Math.min(...positions.map((p) => p.y));
  const graphWidth = Math.max(...positions.map((p) => p.x)) + NODE_WIDTH - left;
  const graphHeight = Math.max(...positions.map((p) => p.y)) + NODE_HEIGHT - top;
  const fit = (room: number, size: number) => (room - 2 * PADDING) / size;
  const clamp = (zoom: number) => Math.max(minZoom, Math.min(MAX_ZOOM, zoom));
  let zoom = clamp(fit(width, graphWidth));
  const height = Math.max(
    MIN_CANVAS_HEIGHT,
    Math.min(MAX_CANVAS_HEIGHT, graphHeight * zoom + 2 * PADDING),
  );
  zoom = clamp(Math.min(zoom, fit(height, graphHeight)));
  // Centred where it fits; otherwise from its left (where it starts) or top edge.
  const offset = (room: number, size: number, start: number) =>
    Math.max(PADDING, (room - size * zoom) / 2) - start * zoom;
  return {
    height,
    viewport: { x: offset(width, graphWidth, left), y: offset(height, graphHeight, top), zoom },
  };
}
