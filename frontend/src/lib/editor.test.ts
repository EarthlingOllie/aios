import { describe, expect, it } from "vitest";

import type { WorkflowNode } from "@/lib/api";
import {
  addNode,
  canConnect,
  connect,
  findCycle,
  fromVersion,
  keyError,
  moveNodes,
  removeEdge,
  removeNode,
  renameNode,
  retarget,
  setCondition,
  setNodeSpec,
  setSchemas,
  toInput,
  updateToLatest,
  type Draft,
  type Target,
} from "@/lib/editor";
import { layoutGraph, NODE_HEIGHT, NODE_WIDTH } from "@/lib/graph";

const writer: Target = { kind: "agent", id: 1, name: "Writer", version: 2, version_id: 12 };
const review: Target = { kind: "workflow", id: 7, name: "Peer review", version: 1, version_id: 70 };

function node(key: string, patch: Partial<WorkflowNode> = {}): WorkflowNode {
  return {
    key,
    kind: "agent",
    version_id: 11,
    target_id: 1,
    name: "Writer",
    version: 1,
    latest_version: 2,
    latest_version_id: 12,
    ...patch,
  };
}

const chain: Draft = {
  nodes: [node("a"), node("b"), node("c")],
  edges: [
    { source: "a", target: "b" },
    { source: "b", target: "c" },
  ],
  positions: { a: { x: 0, y: 0 }, b: { x: 300, y: 0 }, c: { x: 600, y: 0 } },
};

describe("fromVersion", () => {
  it("places every node, keeping saved positions", () => {
    const draft = fromVersion({ nodes: chain.nodes, edges: chain.edges }, { b: { x: 5, y: 6 } });
    expect(Object.keys(draft.positions).sort()).toEqual(["a", "b", "c"]);
    expect(draft.positions.b).toEqual({ x: 5, y: 6 });
  });
});

describe("findCycle", () => {
  it("names a cycle, or null", () => {
    expect(findCycle(["a", "b", "c"], chain.edges)).toBeNull();
    expect(findCycle(["a", "b", "c"], [...chain.edges, { source: "c", target: "a" }])).toEqual([
      "a",
      "b",
      "c",
      "a",
    ]);
  });
  it("follows every edge out of a node, and only edges between the given keys", () => {
    const edges = [
      { source: "a", target: "b" },
      { source: "a", target: "c" },
      { source: "c", target: "a" },
      { source: "x", target: "b" },
      { source: "b", target: "x" },
    ];
    expect(findCycle(["a", "b", "c"], edges)).toEqual(["a", "c", "a"]);
    expect(findCycle(["a", "b"], edges)).toBeNull();
  });
});

describe("keyError", () => {
  it("checks keys like the server does", () => {
    expect(keyError("ok_key-1", [])).toBeNull();
    expect(keyError("", [])).toMatch(/1-64 letters/);
    expect(keyError("has space", [])).toMatch(/1-64 letters/);
    expect(keyError("x".repeat(65), [])).toMatch(/1-64 letters/);
    expect(keyError("a", ["a"])).toMatch(/already used/);
  });
});

describe("addNode", () => {
  it("adds a node keyed after its target's name", () => {
    const draft = addNode(chain, writer);
    const added = draft.nodes.at(-1)!;
    expect(added).toMatchObject({
      key: "writer",
      kind: "agent",
      version_id: 12,
      target_id: 1,
      version: 2,
      latest_version: 2,
    });
    expect(draft.positions.writer).toBeDefined();
    // Keys stay unique.
    expect(addNode(draft, writer).nodes.at(-1)!.key).toBe("writer-2");
    expect(addNode(chain, review).nodes.at(-1)!.key).toBe("peer-review");
  });

  it("falls back to the kind when a name has nothing usable for a key", () => {
    const odd = { ...writer, name: "✨✨" };
    expect(addNode(chain, odd).nodes.at(-1)!.key).toBe("agent");
  });
});

describe("laying out again", () => {
  // The chain, with b dragged up and to the far right, past c.
  const dragged = moveNodes(chain, { b: { x: 900, y: -200 } });
  const fresh = ({ nodes, edges }: Draft) =>
    layoutGraph(
      nodes.map((n) => n.key),
      edges,
      {},
    );
  const leftToRight = (d: Draft, keys: string[]) =>
    keys.slice(1).forEach((key, i) => {
      expect(d.positions[key].x).toBeGreaterThan(d.positions[keys[i]].x);
    });

  it("happens when an edge is added or removed, resetting dragged positions", () => {
    // a → b → c, then writer added and put between b and c.
    const added = addNode(dragged, writer);
    const between = removeEdge(connect(connect(added, "b", "writer"), "writer", "c"), "b", "c");
    expect(between.positions).toEqual(fresh(between));
    leftToRight(between, ["a", "b", "writer", "c"]);
    expect(removeEdge(dragged, "a", "b").positions).toEqual(fresh(removeEdge(dragged, "a", "b")));
  });

  it("happens when a node is added or removed", () => {
    const added = addNode(dragged, writer);
    expect(added.positions).toEqual(fresh(added));
    // An unconnected node gets a place of its own, overlapping none.
    const { writer: spot, ...rest } = added.positions;
    for (const p of Object.values(rest)) {
      expect(Math.abs(p.x - spot.x) >= NODE_WIDTH || Math.abs(p.y - spot.y) >= NODE_HEIGHT).toBe(
        true,
      );
    }
    const removed = removeNode(dragged, "c");
    expect(removed.positions).toEqual(fresh(removed));
  });

  it("doesn't happen when nodes are moved, renamed or pointed elsewhere", () => {
    expect(renameNode(dragged, "a", "start").positions.start).toEqual(chain.positions.a);
    expect(retarget(dragged, "b", review).positions).toBe(dragged.positions);
    expect(updateToLatest(dragged, "b").positions).toBe(dragged.positions);
    expect(dragged.positions.b).toEqual({ x: 900, y: -200 });
  });

  it("doesn't happen when an edge is refused", () => {
    expect(connect(dragged, "c", "a")).toBe(dragged);
  });
});

describe("removeNode", () => {
  it("removes the node with its edges and position", () => {
    const draft = removeNode(chain, "b");
    expect(draft.nodes.map((n) => n.key)).toEqual(["a", "c"]);
    expect(draft.edges).toEqual([]);
    expect(draft.positions.b).toBeUndefined();
  });
});

describe("renameNode", () => {
  it("renames the key everywhere", () => {
    const draft = renameNode(chain, "b", "middle");
    expect(draft.nodes.map((n) => n.key)).toEqual(["a", "middle", "c"]);
    expect(draft.edges).toEqual([
      { source: "a", target: "middle" },
      { source: "middle", target: "c" },
    ]);
    expect(draft.positions.middle).toEqual({ x: 300, y: 0 });
    expect(draft.positions.b).toBeUndefined();
  });
});

describe("retarget / updateToLatest", () => {
  it("points a node at another agent or workflow, at its latest version", () => {
    const draft = retarget(chain, "a", review);
    expect(draft.nodes[0]).toMatchObject({
      key: "a",
      kind: "workflow",
      version_id: 70,
      target_id: 7,
      name: "Peer review",
      version: 1,
    });
  });

  it("takes a node's latest version", () => {
    const draft = updateToLatest(chain, "a");
    expect(draft.nodes[0]).toMatchObject({ version_id: 12, version: 2 });
  });
});

describe("connect", () => {
  it("adds an edge when it's allowed", () => {
    const draft = connect(chain, "a", "c");
    expect(draft.edges).toContainEqual({ source: "a", target: "c" });
  });

  it("refuses self-loops, duplicates and cycles, saying why", () => {
    expect(canConnect(chain, "a", "a")).toMatch(/itself/);
    expect(canConnect(chain, "a", "b")).toMatch(/already/);
    expect(canConnect(chain, "c", "a")).toBe("That would make a cycle: a → b → c → a");
    expect(canConnect(chain, "a", "c")).toBeNull();
    expect(connect(chain, "c", "a")).toBe(chain);
  });
});

describe("removeEdge / moveNodes", () => {
  it("removes one edge", () => {
    expect(removeEdge(chain, "a", "b").edges).toEqual([{ source: "b", target: "c" }]);
  });
  it("moves several nodes at once, leaving the rest and the nodes and edges as they were", () => {
    const moved = moveNodes(chain, { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
    expect(moved.positions).toEqual({ ...chain.positions, a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
    // The editor only works out what a node can connect to again when these change.
    expect(moved.nodes).toBe(chain.nodes);
    expect(moved.edges).toBe(chain.edges);
  });
});

describe("toInput", () => {
  it("is the save request, with every node's position", () => {
    expect(toInput(chain, { name: "W", description: "d", note: " " })).toEqual({
      name: "W",
      description: "d",
      nodes: [
        { key: "a", kind: "agent", version_id: 11 },
        { key: "b", kind: "agent", version_id: 11 },
        { key: "c", kind: "agent", version_id: 11 },
      ],
      edges: chain.edges,
      layout: chain.positions,
      note: undefined,
    });
  });
});

describe("structured workflows in the draft", () => {
  const score = { type: "object", properties: { score: { type: "number" } } };
  const branching: Draft = {
    nodes: [node("a"), node("b", { input_template: "Why: {{nodes.a.reason}} {{ nodes.a }}" })],
    edges: [{ source: "a", target: "b", condition: { ">": [{ var: "output.score" }, 5] } }],
    positions: {},
  };

  it("sets what a node declares, an edge's condition and the workflow's schemas", () => {
    let draft = setNodeSpec(branching, "a", { output_schema: score });
    draft = setCondition(draft, "a", "b", null);
    draft = setSchemas(draft, { input_schema: { type: "string" } });
    const input = toInput(draft, { name: "W", description: "" });
    expect(input.nodes[0]).toEqual({ key: "a", kind: "agent", version_id: 11, output_schema: score });
    expect(input.nodes[1].input_template).toBe("Why: {{nodes.a.reason}} {{ nodes.a }}");
    expect(input.edges).toEqual([{ source: "a", target: "b" }]);
    expect(input.input_schema).toEqual({ type: "string" });
    expect(input).not.toHaveProperty("output_schema");
  });

  it("keeps conditions, and renames the templates that name a renamed node", () => {
    const renamed = renameNode(branching, "a", "scorer");
    expect(renamed.edges).toEqual([
      { source: "scorer", target: "b", condition: { ">": [{ var: "output.score" }, 5] } },
    ]);
    expect(renamed.nodes[1].input_template).toBe("Why: {{nodes.scorer.reason}} {{ nodes.scorer }}");
    // A key that only starts the same isn't renamed.
    const other = renameNode({ ...branching, nodes: [...branching.nodes, node("ab")] }, "ab", "x");
    expect(other.nodes[1].input_template).toBe(branching.nodes[1].input_template);
  });

  it("starts from a version's schemas", () => {
    const draft = fromVersion({ nodes: [], edges: [], input_schema: score }, {});
    expect(draft.input_schema).toEqual(score);
    expect(draft.output_schema).toBeNull();
  });
});
