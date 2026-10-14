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
  toInput,
  updateToLatest,
  type Draft,
  type Target,
} from "@/lib/editor";

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
  it("adds a node keyed after its target's name, at the given position", () => {
    const draft = addNode(chain, writer, { x: 9, y: 9 });
    const added = draft.nodes.at(-1)!;
    expect(added).toMatchObject({
      key: "writer",
      kind: "agent",
      version_id: 12,
      target_id: 1,
      version: 2,
      latest_version: 2,
    });
    expect(draft.positions.writer).toEqual({ x: 9, y: 9 });
    // Keys stay unique.
    expect(addNode(draft, writer, { x: 0, y: 0 }).nodes.at(-1)!.key).toBe("writer-2");
    expect(addNode(chain, review, { x: 0, y: 0 }).nodes.at(-1)!.key).toBe("peer-review");
  });

  it("falls back to the kind when a name has nothing usable for a key", () => {
    const odd = { ...writer, name: "✨✨" };
    expect(addNode(chain, odd, { x: 0, y: 0 }).nodes.at(-1)!.key).toBe("agent");
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
  it("moves several nodes at once, leaving the rest", () => {
    const moved = moveNodes(chain, { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
    expect(moved.positions).toEqual({ ...chain.positions, a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
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
