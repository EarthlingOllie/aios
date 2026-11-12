/**
 * The workflow editor's draft: the graph being edited, with a position for every node, and the
 * edits that can be made to it. Each edit returns a new draft (or the same one, if it can't be
 * made), so the editor can keep it in state and compare it with what was loaded.
 */
import type {
  JsonValue,
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
  /** JSON Schemas for what the workflow's runs take and give, if it declares them. */
  input_schema?: JsonValue | null;
  output_schema?: JsonValue | null;
};

/** What a node can declare (`setNodeSpec`): its schemas and input template, and an approval
 * node's prompt, approver role and expiry. */
export type NodeSpec = Partial<
  Pick<
    WorkflowNode,
    | "input_schema"
    | "output_schema"
    | "input_template"
    | "prompt_template"
    | "approver_role"
    | "expiry_days"
  >
>;

/** An agent or workflow a node can run, at its current version. */
export type Target = {
  kind: WorkflowNodeKind;
  id: number;
  name: string;
  version: number;
  version_id: number;
  /** Why this workflow can't run it at that version, if it can't: it's this one, or runs it. */
  unavailable?: "Current workflow" | "Runs this workflow";
};

const KEY_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/** A version's graph as a draft: saved positions where there are some, the rest laid out. */
export function fromVersion(
  version: {
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
    input_schema?: JsonValue | null;
    output_schema?: JsonValue | null;
  },
  layout: Record<string, NodePosition>,
): Draft {
  const keys = version.nodes.map((n) => n.key);
  return {
    nodes: version.nodes,
    edges: version.edges,
    positions: layoutGraph(keys, version.edges, layout),
    input_schema: version.input_schema ?? null,
    output_schema: version.output_schema ?? null,
  };
}

/**
 * The draft laid out afresh, every node where the layout puts it. Adding or removing a node or an
 * edge does this, so the graph keeps reading left to right in the order it runs (rather than,
 * say, a new node staying wherever it was first put); positions dragged by hand are reset then.
 */
function relaid(draft: Draft): Draft {
  const keys = draft.nodes.map((n) => n.key);
  return { ...draft, positions: layoutGraph(keys, draft.edges, {}) };
}

/** Why `key` can't name a node (`others` are the other nodes' keys), or null if it can. */
export function keyError(key: string, others: string[]): string | null {
  if (!KEY_PATTERN.test(key)) return "Use 1-64 letters, digits, '-' or '_'.";
  if (others.includes(key)) return `The key “${key}” is already used by another node.`;
  return null;
}

/** A cycle the edges make, as the keys around it with the first repeated at the end; or null. */
export function findCycle(keys: string[], edges: WorkflowEdge[]): string[] | null {
  const next = new Map(keys.map((k) => [k, [] as string[]]));
  for (const e of edges) next.get(e.source)?.push(e.target);
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

export function addNode(draft: Draft, target: Target): Draft {
  const key = newKey(
    target,
    draft.nodes.map((n) => n.key),
  );
  return relaid({ ...draft, nodes: [...draft.nodes, { key, ...pin(target) }] });
}

/** A new approval node, decided by an editor (or an admin) unless set otherwise. */
export function addApprovalNode(draft: Draft): Draft {
  const taken = draft.nodes.map((n) => n.key);
  let key = "approval";
  for (let n = 2; taken.includes(key); n++) key = `approval-${n}`;
  const node: WorkflowNode = {
    key,
    kind: "approval",
    version_id: null,
    target_id: null,
    name: "Approval",
    version: null,
    latest_version: null,
    latest_version_id: null,
    prompt_template: "",
    approver_role: "editor",
    expiry_days: null,
  };
  return relaid({ ...draft, nodes: [...draft.nodes, node] });
}

export function removeNode(draft: Draft, key: string): Draft {
  return relaid({
    ...draft,
    nodes: draft.nodes.filter((n) => n.key !== key),
    edges: draft.edges.filter((e) => e.source !== key && e.target !== key),
    positions: {},
  });
}

/** A template with its placeholders for node `from` naming `to` instead. */
function renameInTemplate(template: JsonValue | null | undefined, from: string, to: string) {
  if (template === null || template === undefined) return template;
  const pattern = new RegExp(`\\{\\{(\\s*)nodes\\.${from.replace(/-/g, "\\-")}(?=[.\\s}])`, "g");
  const walk = (value: JsonValue): JsonValue =>
    typeof value === "string"
      ? value.replace(pattern, `{{$1nodes.${to}`)
      : Array.isArray(value)
        ? value.map(walk)
        : value !== null && typeof value === "object"
          ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, walk(v)]))
          : value;
  return walk(template);
}

/** Renames a node's key, in its edges, position and the templates that name it too. Check it
 * with `keyError` first. */
export function renameNode(draft: Draft, from: string, to: string): Draft {
  const rename = (key: string) => (key === from ? to : key);
  return {
    ...draft,
    nodes: draft.nodes.map((n) => {
      let renamed = n.key === from ? { ...n, key: to } : n;
      if (n.prompt_template) {
        const prompt = renameInTemplate(n.prompt_template, from, to) as string;
        renamed = { ...renamed, prompt_template: prompt };
      }
      return n.input_template === null || n.input_template === undefined
        ? renamed
        : { ...renamed, input_template: renameInTemplate(n.input_template, from, to) };
    }),
    edges: draft.edges.map((e) => ({ ...e, source: rename(e.source), target: rename(e.target) })),
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
      n.key === key &&
      n.kind !== "approval" &&
      n.latest_version !== null &&
      n.latest_version_id !== null
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
  return relaid({ ...draft, edges: [...draft.edges, { source, target }] });
}

export function removeEdge(draft: Draft, source: string, target: string): Draft {
  return relaid({
    ...draft,
    edges: draft.edges.filter((e) => !(e.source === source && e.target === target)),
  });
}

/** Sets what a node declares: its schemas and its input template (null: none). */
export function setNodeSpec(draft: Draft, key: string, spec: NodeSpec): Draft {
  return {
    ...draft,
    nodes: draft.nodes.map((n) => (n.key === key ? { ...n, ...spec } : n)),
  };
}

/** Sets an edge's condition (null: none, the edge is always taken). */
export function setCondition(
  draft: Draft,
  source: string,
  target: string,
  condition: JsonValue | null,
): Draft {
  return {
    ...draft,
    edges: draft.edges.map((e) =>
      e.source === source && e.target === target ? { ...e, condition } : e,
    ),
  };
}

/** Sets the workflow's input or output schema (null: none). */
export function setSchemas(
  draft: Draft,
  schemas: Pick<Draft, "input_schema" | "output_schema">,
): Draft {
  return { ...draft, ...schemas };
}

/** Moves nodes, all at once, e.g. the several selected nodes in one drag. */
export function moveNodes(draft: Draft, positions: Record<string, NodePosition>): Draft {
  return { ...draft, positions: { ...draft.positions, ...positions } };
}

/** The fields of `fields` that are set. */
function declared<T extends Record<string, JsonValue | null | undefined>>(fields: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(fields).filter(([, v]) => v !== null && v !== undefined),
  ) as Partial<T>;
}

/** The save request for the draft, with every node's position as the layout. */
export function toInput(
  draft: Draft,
  meta: { name: string; description: string; note?: string },
): WorkflowInput {
  return {
    name: meta.name,
    description: meta.description,
    // What's declared only where it is, so a workflow without any saves as it always has.
    nodes: draft.nodes.map(
      ({ key, kind, version_id, input_schema, output_schema, input_template, ...approval }) => ({
        key,
        kind,
        version_id: kind === "approval" ? null : version_id,
        ...declared({ input_schema, output_schema, input_template }),
        // An approval node's own fields: its prompt and role always, its expiry if it sets one.
        ...(kind === "approval" && {
          prompt_template: approval.prompt_template ?? "",
          approver_role: approval.approver_role ?? "editor",
          ...declared({ expiry_days: approval.expiry_days }),
        }),
      }),
    ),
    edges: draft.edges.map(({ source, target, condition }) => ({
      source,
      target,
      ...declared({ condition }),
    })),
    ...declared({ input_schema: draft.input_schema, output_schema: draft.output_schema }),
    layout: draft.positions,
    note: meta.note?.trim() || undefined,
  };
}
