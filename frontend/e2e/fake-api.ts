import type { Page, Route } from "@playwright/test";

/**
 * An in-memory stand-in for the backend's agents and workflows API, enough for the browser
 * tests: it keeps state across requests, so a test can save and see the result, and follows
 * the server's rules (a changed graph is a new version, the layout isn't versioned, nodes pin
 * versions that must exist, a workflow can't contain itself, a save based on an older version
 * is refused).
 */

type Kind = "agent" | "workflow";
type NodeIn = { key: string; kind: Kind; version_id: number };
type Edge = { source: string; target: string };
type Position = { x: number; y: number };

type AgentRow = { id: number; name: string; description: string; versions: number[] };
type WorkflowVersionRow = {
  id: number;
  workflow_id: number;
  version: number;
  note: string | null;
  created_at: string;
  nodes: NodeIn[];
  edges: Edge[];
};
type WorkflowRow = {
  id: number;
  name: string;
  description: string;
  layout: Record<string, Position>;
  versions: number[];
  deleted: boolean;
  created_at: string;
  updated_at: string;
};

const NOW = "2026-10-14T09:00:00Z";

export class FakeApi {
  private nextId = 100;
  agents = new Map<number, AgentRow>();
  // Agent version id -> [agent id, version number].
  agentVersions = new Map<number, [number, number]>();
  workflows = new Map<number, WorkflowRow>();
  workflowVersions = new Map<number, WorkflowVersionRow>();
  /** Every write the app sent, for assertions: [method, path, body]. */
  writes: [string, string, unknown][] = [];

  private id() {
    return this.nextId++;
  }

  addAgent(name: string, versions = 1, description = ""): AgentRow {
    const agent: AgentRow = { id: this.id(), name, description, versions: [] };
    for (let n = 1; n <= versions; n++) {
      const vid = this.id();
      agent.versions.push(vid);
      this.agentVersions.set(vid, [agent.id, n]);
    }
    this.agents.set(agent.id, agent);
    return agent;
  }

  /** Adds a workflow; `nodes` name their target as `{ agent }` or `{ workflow }`, at its latest. */
  addWorkflow(
    name: string,
    nodes: { key: string; agent?: AgentRow; workflow?: WorkflowRow; version?: number }[] = [],
    edges: [string, string][] = [],
    layout: Record<string, Position> = {},
  ): WorkflowRow {
    const workflow: WorkflowRow = {
      id: this.id(),
      name,
      description: "",
      layout,
      versions: [],
      deleted: false,
      created_at: NOW,
      updated_at: NOW,
    };
    this.workflows.set(workflow.id, workflow);
    this.saveVersion(
      workflow,
      nodes.map((n) => {
        const target = (n.agent ?? n.workflow)!;
        const version_id = target.versions[(n.version ?? target.versions.length) - 1];
        return { key: n.key, kind: n.agent ? "agent" : "workflow", version_id };
      }),
      edges.map(([source, target]) => ({ source, target })),
      null,
    );
    return workflow;
  }

  /** A new version of `workflow` with the same graph as its current one, plus `change`. */
  addWorkflowVersion(
    workflow: WorkflowRow,
    change: (v: WorkflowVersionRow) => void,
    note: string | null = null,
  ) {
    const current = this.current(workflow);
    const next = structuredClone(current);
    change(next);
    this.saveVersion(workflow, next.nodes, next.edges, note);
  }

  /** A newer version of the agent, as editing it would make. */
  bumpAgent(agent: AgentRow) {
    const vid = this.id();
    agent.versions.push(vid);
    this.agentVersions.set(vid, [agent.id, agent.versions.length]);
  }

  current(workflow: WorkflowRow) {
    return this.workflowVersions.get(workflow.versions.at(-1)!)!;
  }

  private saveVersion(workflow: WorkflowRow, nodes: NodeIn[], edges: Edge[], note: string | null) {
    const hash = (n: NodeIn[], e: Edge[]) =>
      JSON.stringify([
        n.map((x) => `${x.key}/${x.kind}/${x.version_id}`).sort(),
        e.map((x) => `${x.source}>${x.target}`).sort(),
      ]);
    if (workflow.versions.length) {
      const cur = this.current(workflow);
      if (hash(cur.nodes, cur.edges) === hash(nodes, edges)) return;
    }
    const version: WorkflowVersionRow = {
      id: this.id(),
      workflow_id: workflow.id,
      version: workflow.versions.length + 1,
      note,
      created_at: NOW,
      nodes,
      edges,
    };
    this.workflowVersions.set(version.id, version);
    workflow.versions.push(version.id);
  }

  // --- Responses, shaped like the server's -------------------------------------------------

  private nodeOut(node: NodeIn) {
    if (node.kind === "agent") {
      const [agentId, version] = this.agentVersions.get(node.version_id)!;
      const agent = this.agents.get(agentId)!;
      return {
        ...node,
        target_id: agentId,
        name: agent.name,
        version,
        latest_version: agent.versions.length,
        latest_version_id: agent.versions.at(-1)!,
      };
    }
    const pinned = this.workflowVersions.get(node.version_id)!;
    const target = this.workflows.get(pinned.workflow_id)!;
    return {
      ...node,
      target_id: target.id,
      name: target.name,
      version: pinned.version,
      latest_version: target.deleted ? null : target.versions.length,
      latest_version_id: target.deleted ? null : target.versions.at(-1)!,
    };
  }

  versionOut(version: WorkflowVersionRow) {
    return {
      id: version.id,
      version: version.version,
      note: version.note,
      created_at: version.created_at,
      nodes: version.nodes.map((n) => this.nodeOut(n)),
      edges: version.edges,
    };
  }

  workflowOut(workflow: WorkflowRow) {
    return {
      id: workflow.id,
      name: workflow.name,
      description: workflow.description,
      created_at: workflow.created_at,
      updated_at: workflow.updated_at,
      current_version: this.versionOut(this.current(workflow)),
      layout: workflow.layout,
    };
  }

  /** The workflows whose versions `version` runs, at any depth. */
  private contains(version: WorkflowVersionRow, found = new Set<number>()) {
    for (const n of version.nodes) {
      if (n.kind !== "workflow") continue;
      const pinned = this.workflowVersions.get(n.version_id)!;
      found.add(pinned.workflow_id);
      this.contains(pinned, found);
    }
    return found;
  }

  private live() {
    return [...this.workflows.values()]
      .filter((w) => !w.deleted)
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  // --- Routing ---------------------------------------------------------------------------------

  async install(page: Page) {
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      (route) => this.handle(route),
    );
  }

  private async handle(route: Route) {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;
    const body = request.postDataJSON?.() ?? null;
    if (method !== "GET") this.writes.push([method, path, body]);
    const reply = (status: number, json?: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: json === undefined ? "" : JSON.stringify(json),
      });
    const fail = (status: number, detail: string) => reply(status, { detail });
    let m: RegExpMatchArray | null;

    if (path === "/api/me") {
      return reply(200, { id: 1, login: "tester", name: "Tester", avatar_url: null });
    }
    if (path === "/api/documents") return reply(200, []);
    if (path === "/api/agents") {
      return reply(
        200,
        [...this.agents.values()].map((a) => ({
          id: a.id,
          name: a.name,
          description: a.description,
          version: a.versions.length,
          version_id: a.versions.at(-1),
          skill_count: 0,
          tool_count: 0,
          run_count: 0,
          updated_at: NOW,
        })),
      );
    }
    if (path === "/api/workflows" && method === "GET") {
      return reply(
        200,
        this.live().map((w) => ({
          id: w.id,
          name: w.name,
          description: w.description,
          version: w.versions.length,
          version_id: w.versions.at(-1),
          node_count: this.current(w).nodes.length,
          updated_at: w.updated_at,
          contains: [...this.contains(this.current(w))],
        })),
      );
    }
    if (path === "/api/workflows" && method === "POST") {
      if (this.live().some((w) => w.name === body.name)) {
        return fail(409, "You already have a workflow with that name");
      }
      const error = this.check(body);
      if (error) return fail(422, error);
      const workflow: WorkflowRow = {
        id: this.id(),
        name: body.name,
        description: body.description ?? "",
        layout: {},
        versions: [],
        deleted: false,
        created_at: NOW,
        updated_at: NOW,
      };
      this.workflows.set(workflow.id, workflow);
      this.applyLayout(workflow, body);
      this.saveVersion(workflow, body.nodes, body.edges, body.note ?? null);
      return reply(201, this.workflowOut(workflow));
    }
    if ((m = path.match(/^\/api\/workflows\/(\d+)(\/.*)?$/))) {
      const workflow = this.workflows.get(Number(m[1]));
      if (!workflow || workflow.deleted) return fail(404, "Not Found");
      const rest = m[2] ?? "";
      if (rest === "" && method === "GET") return reply(200, this.workflowOut(workflow));
      if (rest === "" && method === "PUT") {
        const now = workflow.versions.length;
        if (body.base_version != null && body.base_version !== now) {
          return fail(
            409,
            `This workflow has changed since you started editing (now v${now}). Reload to see the changes.`,
          );
        }
        if (this.live().some((w) => w.name === body.name && w !== workflow)) {
          return fail(409, "You already have a workflow with that name");
        }
        const error = this.check(body, workflow);
        if (error) return fail(422, error);
        workflow.name = body.name;
        workflow.description = body.description ?? "";
        this.applyLayout(workflow, body);
        this.saveVersion(workflow, body.nodes, body.edges, body.note ?? null);
        return reply(200, this.workflowOut(workflow));
      }
      if (rest === "" && method === "DELETE") {
        const users = this.live().filter((w) =>
          this.current(w).nodes.some(
            (n) =>
              n.kind === "workflow" &&
              this.workflowVersions.get(n.version_id)!.workflow_id === workflow.id,
          ),
        );
        if (users.length) {
          return fail(
            409,
            `Remove this workflow from these workflows first: ${users.map((w) => w.name).join(", ")}.`,
          );
        }
        workflow.deleted = true;
        return reply(204);
      }
      if (rest === "/versions") {
        return reply(
          200,
          [...workflow.versions].reverse().map((vid) => {
            const v = this.workflowVersions.get(vid)!;
            return {
              id: v.id,
              version: v.version,
              note: v.note,
              created_at: v.created_at,
              node_count: v.nodes.length,
            };
          }),
        );
      }
      if ((m = rest.match(/^\/versions\/(\d+)(\/restore)?$/))) {
        const vid = workflow.versions[Number(m[1]) - 1];
        if (vid === undefined) return fail(404, "Not Found");
        const old = this.workflowVersions.get(vid)!;
        if (!m[2]) return reply(200, this.versionOut(old));
        this.saveVersion(workflow, old.nodes, old.edges, `Restored from v${old.version}`);
        return reply(200, this.workflowOut(workflow));
      }
    }
    return fail(404, `fake API: no route for ${method} ${path}`);
  }

  private applyLayout(workflow: WorkflowRow, body: { nodes: NodeIn[]; layout?: unknown }) {
    const layout = (body.layout as Record<string, Position> | undefined) ?? workflow.layout;
    const keys = new Set(body.nodes.map((n) => n.key));
    workflow.layout = Object.fromEntries(Object.entries(layout).filter(([k]) => keys.has(k)));
  }

  /** The server's validation, roughly: why the graph can't be saved, if it can't. */
  private check(body: { nodes: NodeIn[]; edges: Edge[] }, workflow?: WorkflowRow) {
    const keys = body.nodes.map((n) => n.key);
    for (const key of keys) {
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(key)) {
        return `Invalid node key '${key}': use 1-64 letters, digits, '-' or '_'.`;
      }
    }
    if (new Set(keys).size !== keys.length) return "Each node key can be used only once";
    for (const n of body.nodes) {
      if (n.kind === "agent" && !this.agentVersions.has(n.version_id)) {
        return "Unknown agent version";
      }
      if (n.kind === "workflow") {
        const v = this.workflowVersions.get(n.version_id);
        if (!v) return "Unknown workflow version";
        if (workflow && v.workflow_id === workflow.id) return "A workflow can't contain itself";
        if (workflow && this.contains(v).has(workflow.id)) {
          return "A workflow can't contain itself, even through another workflow";
        }
      }
    }
    return null;
  }
}

/** Installs a fake API on the page and returns it, for seeding and assertions. */
export async function fakeApi(page: Page): Promise<FakeApi> {
  const api = new FakeApi();
  await api.install(page);
  return api;
}
