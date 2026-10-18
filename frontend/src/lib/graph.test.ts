import { describe, expect, it } from "vitest";

import {
  fitGraph,
  MIN_EDITING_ZOOM,
  layoutGraph,
  MAX_CANVAS_HEIGHT,
  MIN_CANVAS_HEIGHT,
  MIN_ZOOM,
  NODE_HEIGHT,
  NODE_WIDTH,
  startKeys,
} from "@/lib/graph";

const edges = [
  { source: "a", target: "b" },
  { source: "a", target: "c" },
  { source: "b", target: "d" },
  { source: "c", target: "d" },
];

describe("startKeys", () => {
  it("finds nodes with no incoming edge, in node order", () => {
    expect(startKeys(["d", "a", "b", "c", "lone"], edges)).toEqual(["a", "lone"]);
  });
});

describe("layoutGraph", () => {
  it("lays out left to right, each node after its predecessors", () => {
    const pos = layoutGraph(["a", "b", "c", "d"], edges, {});
    expect(pos.a.x).toBeLessThan(pos.b.x);
    expect(pos.b.x).toBe(pos.c.x);
    expect(pos.c.x).toBeLessThan(pos.d.x);
    // Siblings don't overlap.
    expect(Math.abs(pos.b.y - pos.c.y)).toBeGreaterThanOrEqual(NODE_HEIGHT);
  });

  it("keeps saved positions and places only the nodes without one", () => {
    const pos = layoutGraph(["a", "b"], [{ source: "a", target: "b" }], { a: { x: 500, y: 40 } });
    expect(pos.a).toEqual({ x: 500, y: 40 });
    // The new node goes after its predecessor, wherever that was moved to.
    expect(pos.b.x).toBeGreaterThanOrEqual(500 + NODE_WIDTH);
  });

  it("ignores saved positions for nodes not in the graph", () => {
    const pos = layoutGraph(["a"], [], { gone: { x: 1, y: 1 } });
    expect(Object.keys(pos)).toEqual(["a"]);
  });

  it("handles keys that are also names of built-in object properties", () => {
    const keys = ["__proto__", "constructor", "toString", "valueOf"];
    const chain = [
      { source: "__proto__", target: "constructor" },
      { source: "constructor", target: "toString" },
      { source: "toString", target: "valueOf" },
    ];
    // Parsed, as it comes from the API: a literal `__proto__` key would set the prototype.
    const saved = JSON.parse('{"__proto__": {"x": 500, "y": 40}}');
    const pos = layoutGraph(keys, chain, saved);
    expect(Object.keys(pos)).toEqual(keys);
    const [proto, ctor, toString, valueOf] = keys.map((key) => pos[key]);
    expect(proto).toEqual({ x: 500, y: 40 });
    // The rest follow it, each after the one before.
    expect(ctor.x).toBeGreaterThanOrEqual(500 + NODE_WIDTH);
    expect(toString.x).toBeGreaterThan(ctor.x);
    expect(valueOf.x).toBeGreaterThan(toString.x);
    expect([ctor.y, toString.y, valueOf.y].every(Number.isFinite)).toBe(true);
  });

  it("returns an empty layout for an empty graph", () => {
    expect(layoutGraph([], [], {})).toEqual({});
  });
});

describe("fitGraph", () => {
  const row = (n: number) => Array.from({ length: n }, (_, i) => ({ x: i * 264, y: 0 }));
  const column = (n: number) => Array.from({ length: n }, (_, i) => ({ x: 0, y: i * 96 }));

  it("centres a small graph at its own size, on the shortest canvas", () => {
    const { height, viewport } = fitGraph([{ x: 100, y: 50 }], 800);
    expect(height).toBe(MIN_CANVAS_HEIGHT);
    expect(viewport.zoom).toBe(1);
    expect(viewport.x + 100).toBe((800 - NODE_WIDTH) / 2);
    expect(viewport.y + 50).toBe((MIN_CANVAS_HEIGHT - NODE_HEIGHT) / 2);
  });

  it("shrinks a wide graph to fit, but not past legible, then starts it at the left", () => {
    expect(fitGraph(row(3), 700).viewport.zoom).toBeCloseTo((700 - 64) / (2 * 264 + NODE_WIDTH));
    const { viewport } = fitGraph(row(10), 800);
    expect(viewport.zoom).toBe(MIN_ZOOM);
    expect(viewport.x).toBe(32);
  });

  it("shrinks further when told to, e.g. to keep a graph being edited in view", () => {
    const width = 800;
    const zoom = (width - 64) / (5 * 264 + NODE_WIDTH);
    expect(fitGraph(row(6), width).viewport.zoom).toBe(MIN_ZOOM);
    expect(fitGraph(row(6), width, MIN_EDITING_ZOOM).viewport.zoom).toBeCloseTo(zoom);
  });

  it("makes the canvas as tall as a tall graph, up to a limit, then starts it at the top", () => {
    expect(fitGraph(column(5), 800).height).toBe(4 * 96 + NODE_HEIGHT + 64);
    const { height, viewport } = fitGraph(column(20), 800);
    expect(height).toBe(MAX_CANVAS_HEIGHT);
    expect(viewport.zoom).toBe(MIN_ZOOM);
    expect(viewport.y).toBe(32);
  });

  it("has a canvas to add nodes to when there are none", () => {
    expect(fitGraph([], 800).height).toBe(MIN_CANVAS_HEIGHT);
  });

  it("fits a graph into a canvas of a fixed height, e.g. a card's", () => {
    const tall = fitGraph(column(5), 800, 0.1, 160);
    expect(tall.height).toBe(160);
    expect(tall.viewport.zoom).toBeCloseTo((160 - 64) / (4 * 96 + NODE_HEIGHT));
    expect(tall.viewport.y).toBe(32);
    // A small one is centred at its own size, however short the canvas.
    const small = fitGraph([{ x: 0, y: 0 }], 800, 0.1, 160);
    expect(small.height).toBe(160);
    expect(small.viewport.zoom).toBe(1);
    expect(small.viewport.y).toBe((160 - NODE_HEIGHT) / 2);
    expect(fitGraph([], 800, 0.1, 160).height).toBe(160);
  });
});
