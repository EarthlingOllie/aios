import { describe, expect, it } from "vitest";

import { endKeys, layoutGraph, NODE_HEIGHT, NODE_WIDTH, startKeys } from "@/lib/graph";

const edges = [
  { source: "a", target: "b" },
  { source: "a", target: "c" },
  { source: "b", target: "d" },
  { source: "c", target: "d" },
];

describe("startKeys / endKeys", () => {
  it("finds nodes with no incoming or no outgoing edge, in node order", () => {
    expect(startKeys(["d", "a", "b", "c", "lone"], edges)).toEqual(["a", "lone"]);
    expect(endKeys(["d", "a", "b", "c", "lone"], edges)).toEqual(["d", "lone"]);
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
