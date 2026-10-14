/**
 * The workflow editor's draft: the graph being edited, with a position for every node, and the
 * edits that can be made to it. Each edit returns a new draft (or the same one, if it can't be
 * made), so the editor can keep it in state and compare it with what was loaded.
 */
import type {
  NodePosition,
  WorkflowEdge,
  WorkflowInput,
  WorkflowNode,
  WorkflowNodeKind,
} from "@/lib/api";
import { layoutGraph } from "@/lib/graph";

export type Draft = {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  positions: Record<string, NodePosition>;
};

/** An agent or workflow a node can run, at its current version. */
export type Target = {
  kind: WorkflowNodeKind;
  id: number;
  name: string;
  version: number;
  version_id: number;
};

const KEY_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/** A version's graph as a draft: saved positions where there are some, the rest laid out. */
export function fromVersion(
  version: { nodes: WorkflowNode[]; edges: WorkflowEdge[] },
  layout: Record<string, NodePosition>,
): Draft {
  const keys = version.nodes.map((n) => n.key);
  return {
    nodes: version.nodes,
    edges: version.edges,
    positions: layoutGraph(keys, version.edges, layout),
  };
}

/** Why `key` can't name a node (`others` are the other nodes' keys), or null if it can. */
export function keyError(key: string, others: string[]): string | null {
  if (!KEY_PATTERN.test(key)) return "Use 1-64 letters, digits, '-' or '_'.";
  if (others.includes(key)) return `The key “${key}” is already used by another node.`;
  return null;
}

/** A cycle the edges make, as the keys around it with the first repeated at the end; or null. */
export function findCycle(keys: string[], edges: WorkflowEdge[]): string[] | null {
  const next = new Map(
    keys.map((k) => [k, edges.filter((e) => e.source === k).map((e) => e.target)]),
  );
  // Depth-first search; a node still on the stack reached again closes a cycle.
  const state = new Map<string, "open" | "done">();
  const stack: string[] = [];
  const visit = (key: string): string[] | null => {
    state.set(key, "open");
    stack.push(key);
    for (const target of next.get(key) ?? []) {
      if (state.get(target) === "open") return [...stack.slice(stack.indexOf(target)), target];
      if (!state.has(target)) {
        const found = visit(target);
        if (found) return found;
      }
    }
    stack.pop();
    state.set(key, "done");
    return null;
  };
  for (const key of keys) {
    if (!state.has(key)) {
      const found = visit(key);
      if (found) return found;
    }
  }
  return null;
}

/** A key for a new node: its target's name in lowercase-and-dashes, made unique. */
function newKey(target: Target, taken: string[]) {
  const base =
    target.name
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 56) || target.kind;
  let key = base;
  for (let n = 2; taken.includes(key); n++) key = `${base}-${n}`;
  return key;
}

function pin(target: Target): Omit<WorkflowNode, "key"> {
  return {
    kind: target.kind,
    version_id: target.version_id,
    target_id: target.id,
    name: target.name,
    version: target.version,
    latest_version: target.version,
    latest_version_id: target.version_id,
  };
}

export function addNode(draft: Draft, target: Target, position: NodePosition): Draft {
  const key = newKey(
    target,
    draft.nodes.map((n) => n.key),
  );
  return {
    ...draft,
    nodes: [...draft.nodes, { key, ...pin(target) }],
    positions: { ...draft.positions, [key]: position },
  };
}

export function removeNode(draft: Draft, key: string): Draft {
  const { [key]: _removed, ...positions } = draft.positions;
  return {
    nodes: draft.nodes.filter((n) => n.key !== key),
    edges: draft.edges.filter((e) => e.source !== key && e.target !== key),
    positions,
  };
}

/** Renames a node's key, in its edges and position too. Check it with `keyError` first. */
export function renameNode(draft: Draft, from: string, to: string): Draft {
  const rename = (key: string) => (key === from ? to : key);
  return {
    nodes: draft.nodes.map((n) => (n.key === from ? { ...n, key: to } : n)),
    edges: draft.edges.map((e) => ({ source: rename(e.source), target: rename(e.target) })),
    positions: Object.fromEntries(
      Object.entries(draft.positions).map(([key, p]) => [rename(key), p]),
    ),
  };
}

/** Points a node at another agent or workflow, at its current version. */
export function retarget(draft: Draft, key: string, target: Target): Draft {
  return {
    ...draft,
    nodes: draft.nodes.map((n) => (n.key === key ? { key, ...pin(target) } : n)),
  };
}

/** Pins a node to the latest version of what it runs. */
export function updateToLatest(draft: Draft, key: string): Draft {
  return {
    ...draft,
    nodes: draft.nodes.map((n) =>
      n.key === key && n.latest_version !== null && n.latest_version_id !== null
        ? { ...n, version: n.latest_version, version_id: n.latest_version_id }
        : n,
    ),
  };
}

/** Why an edge from `source` to `target` can't be added, or null if it can. */
export function canConnect(draft: Draft, source: string, target: string): string | null {
  if (source === target) return "A node can't lead to itself.";
  if (draft.edges.some((e) => e.source === source && e.target === target)) {
    return `${source} already leads to ${target}.`;
  }
  const cycle = findCycle(
    draft.nodes.map((n) => n.key),
    [...draft.edges, { source, target }],
  );
  return cycle && `That would make a cycle: ${cycle.join(" → ")}`;
}

export function connect(draft: Draft, source: string, target: string): Draft {
  if (canConnect(draft, source, target)) return draft;
  return { ...draft, edges: [...draft.edges, { source, target }] };
}

export function removeEdge(draft: Draft, source: string, target: string): Draft {
  return {
    ...draft,
    edges: draft.edges.filter((e) => !(e.source === source && e.target === target)),
  };
}

/** Moves nodes, all at once, e.g. the several selected nodes in one drag. */
export function moveNodes(draft: Draft, positions: Record<string, NodePosition>): Draft {
  return { ...draft, positions: { ...draft.positions, ...positions } };
}

/** The save request for the draft, with every node's position as the layout. */
export function toInput(
  draft: Draft,
  meta: { name: string; description: string; note?: string },
): WorkflowInput {
  return {
    name: meta.name,
    description: meta.description,
    nodes: draft.nodes.map(({ key, kind, version_id }) => ({ key, kind, version_id })),
    edges: draft.edges,
    layout: draft.positions,
    note: meta.note?.trim() || undefined,
  };
}
