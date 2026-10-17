import type { Page, Route } from "@playwright/test";

/**
 * An in-memory stand-in for the backend's agents and workflows API, enough for the browser
 * tests: it keeps state across requests, so a test can save and see the result, and follows
 * the server's rules (a changed graph is a new version, the layout isn't versioned, nodes pin
 * versions that must exist, a workflow can't contain itself, a save based on an older version
 * is refused), plus schedules' preview and timezones.
 */

type Kind = "agent" | "workflow";
type NodeIn = { key: string; kind: Kind; version_id: number };
type Edge = { source: string; target: string };
type Position = { x: number; y: number };

type AgentRow = {
  id: number;
  name: string;
  description: string;
  base_prompt: string;
  versions: number[];
};
type AgentRunRow = {
  id: number;
  agent_id: number;
  agent_version_id: number;
  title: string | null;
  message_count: number;
  /** Its conversation, as AI SDK UIMessages. */
  ui_messages: unknown[];
};

export type SchemaChangeRow = {
  id: string;
  data_store_id: string;
  status: "pending" | "approved" | "rejected" | "expired" | "applied" | "failed" | "stale" | "cancelled";
  summary: string;
  reason: string;
  sql: string;
  ops: Record<string, unknown>[];
  run_id: number | null;
  tool_call_id: string | null;
  note: string | null;
  error: string | null;
  created_at: string;
  decided_at: string | null;
  applied_at: string | null;
  resumed_at: string | null;
  /** Whether the signed-in user may decide it (the store's owner), while it's pending. */
  decider: boolean;
  /** What approving it takes typed, if it drops anything. */
  confirm_with: string | null;
  /** What its drops would remove (GET .../impact). */
  impact: {
    table: string;
    column: string | null;
    rows: number | null;
    values: number | null;
    columns: string[] | null;
    estimated: boolean;
  }[];
};

export type DroppedRow = {
  id: number;
  kind: "table" | "column";
  table: string;
  column: string | null;
  dropped_name: string;
  dropped_at: string;
  /** How long until it's purged, from now (days). */
  purge_in_days: number;
  blocked: string | null;
  restore_error: string | null;
  /** If set, a restore is refused once it runs (the worker's check), with this. */
  refuseRestore?: string;
};
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

export type DocumentRow = {
  id: number;
  filename: string;
  status: "pending" | "processing" | "ready" | "failed";
  error: string | null;
  chunk_count: number;
  created_at: string;
};
type RunStatus = "queued" | "running" | "succeeded" | "failed";
type StepStatus = "running" | "succeeded" | "failed";
export type StepRow = {
  id: number;
  path: string[];
  kind: Kind;
  status: StepStatus;
  input: string;
  output: string | null;
  error: string | null;
  agent_id: number | null;
  agent_run_id: number | null;
  started_at: string;
  finished_at: string | null;
};
export type RunRow = {
  id: number;
  workflow_id: number;
  workflow_version_id: number;
  status: RunStatus;
  input: string;
  output: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  steps: StepRow[];
};

const NOW = "2026-10-14T09:00:00Z";

export class FakeApi {
  private nextId = 100;
  agents = new Map<number, AgentRow>();
  // Agent version id -> [agent id, version number].
  agentVersions = new Map<number, [number, number]>();
  // Agent version id -> note, for the versions that have one.
  agentNotes = new Map<number, string>();
  workflows = new Map<number, WorkflowRow>();
  workflowVersions = new Map<number, WorkflowVersionRow>();
  runs = new Map<number, RunRow>();
  agentRuns = new Map<number, AgentRunRow>();
  /** The timezones the server knows; it may list ones a browser's Intl doesn't (e.g. "localtime"). */
  timezones = ["America/New_York", "Europe/London", "UTC", "localtime"];
  /** The user's data store's status: documents and search answer 409 until it's "ready". */
  dataStoreStatus: "provisioning" | "ready" | "failed" = "ready";
  /** How long GET /api/data-store takes to answer (ms). */
  dataStoreDelay = 0;
  /** While true, GET /api/data-store answers 500, as if the server can't reach the store. */
  dataStoreFails = false;
  /** The user's documents, newest first. */
  documents: DocumentRow[] = [];
  /** How many times the app asked for the list of documents. */
  documentLists = 0;
  /**
   * The status an upload starts in: "ready" (with one chunk) skips the worker; "pending" waits
   * for the test to finish its ingest with `finishIngest`.
   */
  ingestAs: "ready" | "pending" = "ready";
  /** Uploads of these filenames answer 413, as the proxy does for a file over its size limit. */
  tooLarge = new Set<string>();
  /** While set (by `holdUploads`), uploads wait for `releaseUploads` before they answer. */
  private uploadGate?: { wait: Promise<void>; release: () => void };
  /** How long GET /api/documents takes to answer (ms). */
  documentsDelay = 0;
  /** While true, the documents endpoints answer 503, as when every store connection is in use. */
  dataStoreBusy = false;
  /** Reads of the list being held back by `holdDocumentLists`, oldest first. */
  heldDocumentLists: (() => void)[] = [];
  private holdingLists = false;
  /** Schema change requests in the user's store, oldest first. */
  schemaChanges: SchemaChangeRow[] = [];
  /** Dropped tables and columns in the user's store, newest first. */
  dropped: DroppedRow[] = [];
  /** Every write the app sent, for assertions: [method, path, body]. */
  writes: [string, string, unknown][] = [];

  private id() {
    return this.nextId++;
  }

  /** Adds a document, as if uploaded earlier. */
  addDocument(filename: string, fields: Partial<DocumentRow> = {}): DocumentRow {
    const doc: DocumentRow = {
      id: this.id(),
      filename,
      status: "ready",
      error: null,
      chunk_count: 1,
      created_at: NOW,
      ...fields,
    };
    this.documents.unshift(doc);
    return doc;
  }

  /** Uploads wait, as if still on the wire, until `releaseUploads`. */
  holdUploads() {
    let release = () => {};
    const wait = new Promise<void>((resolve) => (release = resolve));
    this.uploadGate = { wait, release };
  }

  releaseUploads() {
    this.uploadGate?.release();
    this.uploadGate = undefined;
  }

  /**
   * Until called again with false, each GET /api/documents reads the list when it arrives but
   * waits to answer until the test calls its entry in `heldDocumentLists`: a slow response.
   */
  holdDocumentLists(hold = true) {
    this.holdingLists = hold;
  }

  /** The worker finishes ingesting the document called `filename`: into chunks, or failing. */
  finishIngest(filename: string, outcome: { chunks: number } | { error: string }) {
    const doc = this.documents.find((d) => d.filename === filename);
    if (!doc) throw new Error(`No document ${filename}`);
    Object.assign(
      doc,
      "chunks" in outcome
        ? { status: "ready", chunk_count: outcome.chunks, error: null }
        : { status: "failed", chunk_count: 0, error: outcome.error },
    );
  }

  addAgent(name: string, versions = 1, description = ""): AgentRow {
    const agent: AgentRow = {
      id: this.id(),
      name,
      description,
      base_prompt: `You are ${name}.`,
      versions: [],
    };
    for (let n = 1; n <= versions; n++) {
      const vid = this.id();
      agent.versions.push(vid);
      this.agentVersions.set(vid, [agent.id, n]);
    }
    this.agents.set(agent.id, agent);
    return agent;
  }

  /** Adds a chat run of the agent, at its current version. */
  /** Adds a schema change request, pending unless `fields` say otherwise. */
  addSchemaChange(fields: Partial<SchemaChangeRow> = {}): SchemaChangeRow {
    const request: SchemaChangeRow = {
      id: `req-${this.id()}`,
      data_store_id: "store-1",
      status: "pending",
      summary: "Create table data.contacts",
      reason: "To keep track of the people you meet",
      sql: 'CREATE TABLE "data"."contacts" (\n    "id" bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n    "name" text NOT NULL\n);',
      ops: [],
      run_id: null,
      tool_call_id: null,
      note: null,
      error: null,
      created_at: NOW,
      decided_at: null,
      applied_at: null,
      resumed_at: null,
      decider: true,
      confirm_with: null,
      impact: [],
      ...fields,
    };
    this.schemaChanges.push(request);
    return request;
  }

  /** Adds a dropped table or column, as if a drop was approved earlier. */
  addDropped(fields: Partial<DroppedRow> & Pick<DroppedRow, "kind" | "table">): DroppedRow {
    const name = fields.column ?? fields.table;
    const item: DroppedRow = {
      id: this.id(),
      column: null,
      dropped_name: `_dropped_20261014090000_${name}`,
      dropped_at: NOW,
      purge_in_days: 30,
      blocked: null,
      restore_error: null,
      ...fields,
    };
    this.dropped.unshift(item);
    return item;
  }

  private droppedOut(item: DroppedRow) {
    const { purge_in_days, refuseRestore: _, ...fields } = item;
    return { ...fields, purge_at: new Date(Date.now() + purge_in_days * 86400_000).toISOString() };
  }

  /**
   * What the workers do after a decision: the request ends `status` (applied, failed or stale;
   * a rejection keeps its own), and the run that asked carries on, its conversation ending with
   * `messages`.
   */
  finishSchemaChange(
    id: string,
    outcome: { status?: SchemaChangeRow["status"]; error?: string; messages?: unknown[] } = {},
  ) {
    const request = this.schemaChanges.find((r) => r.id === id)!;
    if (outcome.status) request.status = outcome.status;
    if (outcome.error) request.error = outcome.error;
    if (request.status === "applied") request.applied_at = NOW;
    request.resumed_at = NOW;
    const run = request.run_id === null ? undefined : this.agentRuns.get(request.run_id);
    if (run && outcome.messages) run.ui_messages = [...run.ui_messages, ...outcome.messages];
  }

  private schemaChangeOut(request: SchemaChangeRow) {
    const { run_id, decider, impact: _, ...fields } = request;
    const run = run_id === null ? undefined : this.agentRuns.get(run_id);
    return {
      ...fields,
      run: run ? { agent_id: run.agent_id, run_id: run.id, title: run.title } : null,
      can_decide: decider && request.status === "pending",
    };
  }

  addAgentRun(agent: AgentRow, title: string | null = null, message_count = 0): AgentRunRow {
    const run: AgentRunRow = {
      id: this.id(),
      agent_id: agent.id,
      agent_version_id: agent.versions.at(-1)!,
      title,
      message_count,
      ui_messages: [],
    };
    this.agentRuns.set(run.id, run);
    return run;
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

  /** A run of the workflow's current version; steps take their kind from the graph. */
  addRun(
    workflow: WorkflowRow,
    run: Partial<Omit<RunRow, "steps">> & { steps?: (Partial<StepRow> & { path: string[] })[] },
  ): RunRow {
    const row: RunRow = {
      id: this.id(),
      workflow_id: workflow.id,
      workflow_version_id: workflow.versions.at(-1)!,
      status: "running",
      input: "Write the weekly report",
      output: null,
      error: null,
      created_at: NOW,
      started_at: NOW,
      finished_at: null,
      ...run,
      steps: [],
    };
    for (const step of run.steps ?? []) this.addStep(row, step);
    this.runs.set(row.id, row);
    return row;
  }

  /** Starts (or, given a status, records) a step of a run, as the worker would. */
  addStep(run: RunRow, step: Partial<StepRow> & { path: string[] }): StepRow {
    const row: StepRow = {
      id: this.id(),
      kind: "agent",
      status: "running",
      input: run.input,
      output: null,
      error: null,
      agent_id: null,
      agent_run_id: null,
      started_at: NOW,
      finished_at: null,
      ...step,
    };
    run.steps.push(row);
    return row;
  }

  /**
   * Moves a run on by one beat, as the worker would: a queued run starts; the steps running
   * finish; the nodes whose predecessors have all succeeded start; and once nothing is left to
   * run, the run succeeds with the last output. Top-level nodes only, each as one step.
   */
  advance(run: RunRow) {
    const version = this.workflowVersions.get(run.workflow_version_id)!;
    const top = (key: string) => run.steps.find((s) => s.path.length === 1 && s.path[0] === key);
    if (run.status === "queued") Object.assign(run, { status: "running", started_at: NOW });
    for (const step of run.steps) {
      if (step.status !== "running") continue;
      Object.assign(step, {
        status: "succeeded",
        output: `${step.path.at(-1)} done`,
        finished_at: NOW,
      });
    }
    const ready = version.nodes.filter(
      (n) =>
        !top(n.key) &&
        version.edges
          .filter((e) => e.target === n.key)
          .every((e) => top(e.source)?.status === "succeeded"),
    );
    for (const n of ready) {
      const inputs = version.edges
        .filter((e) => e.target === n.key)
        .map((e) => top(e.source)!.output);
      this.addStep(run, {
        path: [n.key],
        kind: n.kind,
        input: inputs.length ? inputs.join("\n") : run.input,
      });
    }
    if (!ready.length) {
      Object.assign(run, {
        status: "succeeded",
        output: run.steps.at(-1)?.output ?? null,
        finished_at: NOW,
      });
    }
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

  private agentRunsOf(agent: AgentRow) {
    return [...this.agentRuns.values()].filter((r) => r.agent_id === agent.id);
  }

  private agentRunOut(run: AgentRunRow) {
    const { ui_messages: _, ...summary } = run;
    return {
      ...summary,
      agent_version: this.agentVersions.get(run.agent_version_id)![1],
      created_at: NOW,
      updated_at: NOW,
    };
  }

  private agentVersionOut(agent: AgentRow, version: number) {
    return {
      id: agent.versions[version - 1],
      version,
      note: this.agentNotes.get(agent.versions[version - 1]) ?? null,
      created_at: NOW,
      base_prompt: agent.base_prompt,
      model: null,
      instructions: agent.base_prompt,
      skills: [],
      tools: [],
    };
  }

  agentOut(agent: AgentRow) {
    return {
      id: agent.id,
      name: agent.name,
      description: agent.description,
      created_at: NOW,
      updated_at: NOW,
      current_version: this.agentVersionOut(agent, agent.versions.length),
    };
  }

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
    // As postDataJSON gives it (`any`), or a placeholder for an upload.
    let body: ReturnType<typeof request.postDataJSON> = null;
    try {
      body = request.postDataJSON?.() ?? null;
    } catch {
      // Not JSON: an upload's multipart form.
      body = "(form)";
    }
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
    if (path === "/api/data-store") {
      await new Promise((resolve) => setTimeout(resolve, this.dataStoreDelay));
      if (this.dataStoreFails) return fail(500, "Internal Server Error");
      return reply(200, { status: this.dataStoreStatus });
    }
    if (path === "/api/documents" && method === "GET") this.documentLists++;
    if (path === "/api/documents" || path.startsWith("/api/documents/") || path === "/api/search") {
      if (this.dataStoreStatus !== "ready") {
        return reply(409, {
          detail:
            this.dataStoreStatus === "failed"
              ? "Your data store couldn't be set up. We'll try again; please check back later."
              : "Your data store is being set up. Try again in a moment.",
          data_store_status: this.dataStoreStatus,
        });
      }
      if (this.dataStoreBusy) return fail(503, "Your data store is busy. Try again in a moment.");
      if (path === "/api/search") {
        // A passage from each ready document whose name contains the query.
        const q = new URL(request.url()).searchParams.get("q")?.toLowerCase() ?? "";
        return reply(
          200,
          this.documents
            .filter((d) => d.status === "ready" && d.filename.toLowerCase().includes(q))
            .map((d, i) => ({
              chunk_id: d.id * 1000,
              document_id: d.id,
              filename: d.filename,
              text: `A passage from ${d.filename}.`,
              meta: {},
              score: 0.03 - i * 0.001,
              semantic_rank: i + 1,
              lexical_rank: i + 1,
            })),
        );
      }
      if ((m = path.match(/^\/api\/documents\/(\d+)$/)) && method === "DELETE") {
        const before = this.documents.length;
        this.documents = this.documents.filter((d) => d.id !== Number(m![1]));
        return this.documents.length < before ? reply(204) : fail(404, "Not Found");
      }
      if (path === "/api/documents" && method === "POST") {
        const filename = /filename="([^"]+)"/.exec(request.postData() ?? "")?.[1] ?? "upload";
        await this.uploadGate?.wait;
        if (this.tooLarge.has(filename)) return reply(413);
        const doc = this.addDocument(
          filename,
          this.ingestAs === "pending" ? { status: "pending", chunk_count: 0 } : {},
        );
        return reply(201, doc);
      }
      if (path === "/api/documents") {
        await new Promise((resolve) => setTimeout(resolve, this.documentsDelay));
        const list = [...this.documents];
        if (this.holdingLists) {
          await new Promise<void>((release) => this.heldDocumentLists.push(release));
        }
        return reply(200, list);
      }
      return fail(404, "Not Found");
    }
    if (path === "/api/schema-changes" && method === "GET") {
      const pending = this.schemaChanges.filter((r) => r.status === "pending");
      const decided = this.schemaChanges.filter((r) => r.status !== "pending").reverse();
      return reply(
        200,
        [...pending, ...decided].map((r) => this.schemaChangeOut(r)),
      );
    }
    if ((m = path.match(/^\/api\/schema-changes\/([\w-]+)\/impact$/)) && method === "GET") {
      const request = this.schemaChanges.find((r) => r.id === m![1]);
      if (!request || !request.decider) return fail(404, "Schema change request not found");
      return reply(200, { removes: request.impact });
    }
    if ((m = path.match(/^\/api\/schema-changes\/([\w-]+)\/(approve|reject)$/)) && method === "POST") {
      const request = this.schemaChanges.find((r) => r.id === m![1]);
      if (!request || !request.decider) return fail(404, "Schema change request not found");
      if (request.status !== "pending") {
        return fail(409, `This request has already been ${request.status}`);
      }
      const typed = (body?.confirm ?? "").trim();
      if (m[2] === "approve" && request.confirm_with && typed !== request.confirm_with) {
        return fail(422, `This request drops data: Type \`${request.confirm_with}\` to confirm.`);
      }
      request.decided_at = NOW;
      if (m[2] === "approve") {
        request.status = "approved";
      } else {
        request.status = "rejected";
        request.note = (body?.note ?? "").trim() || null;
      }
      return reply(200, this.schemaChangeOut(request));
    }
    if (path === "/api/dropped" && method === "GET") {
      return reply(200, this.dropped.map((d) => this.droppedOut(d)));
    }
    if ((m = path.match(/^\/api\/dropped\/(\d+)\/restore$/)) && method === "POST") {
      const item = this.dropped.find((d) => d.id === Number(m![1]));
      if (!item) return fail(404, "Nothing dropped by that id");
      if (item.blocked) return fail(409, item.blocked);
      const out = this.droppedOut(item);
      // What the provisioning worker then does.
      if (item.refuseRestore) item.restore_error = item.refuseRestore;
      else this.dropped = this.dropped.filter((d) => d !== item);
      return reply(202, { ...out, blocked: null });
    }
    if (path === "/api/tools") return reply(200, []);
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
          run_count: this.agentRunsOf(a).length,
          updated_at: NOW,
        })),
      );
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)\/runs\/(\d+)$/)) && method === "DELETE") {
      const run = this.agentRuns.get(Number(m[2]));
      if (!run || run.agent_id !== Number(m[1])) return fail(404, "Not Found");
      this.agentRuns.delete(run.id);
      return reply(204);
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)\/versions\/(\d+)\/restore$/)) && method === "POST") {
      const agent = this.agents.get(Number(m[1]));
      const version = Number(m[2]);
      if (!agent || !agent.versions[version - 1]) return fail(404, "Not Found");
      this.bumpAgent(agent);
      this.agentNotes.set(agent.versions.at(-1)!, `Restored from v${version}`);
      return reply(200, this.agentOut(agent));
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)(\/.*)?$/)) && method === "GET") {
      const agent = this.agents.get(Number(m[1]));
      const rest = m[2] ?? "";
      if (!agent) return fail(404, "Not Found");
      if (rest === "") return reply(200, this.agentOut(agent));
      if (rest === "/runs") {
        return reply(
          200,
          this.agentRunsOf(agent)
            .reverse()
            .map((r) => this.agentRunOut(r)),
        );
      }
      if ((m = rest.match(/^\/runs\/(\d+)$/))) {
        const run = this.agentRuns.get(Number(m[1]));
        if (!run || run.agent_id !== agent.id) return fail(404, "Not Found");
        return reply(200, {
          ...this.agentRunOut(run),
          ui_messages: run.ui_messages,
          messages: [],
          instructions: agent.base_prompt,
          schema_change_requests: this.schemaChanges
            .filter((r) => r.run_id === run.id)
            .map((r) => this.schemaChangeOut(r)),
          waiting: this.schemaChanges.some(
            (r) => r.run_id === run.id && !r.resumed_at && r.status !== "cancelled",
          ),
        });
      }
      if (rest === "/versions") {
        return reply(
          200,
          agent.versions
            .map((vid, i) => ({
              id: vid,
              version: i + 1,
              note: this.agentNotes.get(vid) ?? null,
              created_at: NOW,
              skill_count: 0,
              tool_count: 0,
              run_count: this.agentRunsOf(agent).filter((r) => r.agent_version_id === vid).length,
            }))
            .reverse(),
        );
      }
      if ((m = rest.match(/^\/versions\/(\d+)$/))) {
        const version = Number(m[1]);
        if (!agent.versions[version - 1]) return fail(404, "Not Found");
        return reply(200, this.agentVersionOut(agent, version));
      }
    }
    if (path === "/api/triggers" && method === "GET") return reply(200, []);
    if (path === "/api/triggers/timezones") return reply(200, this.timezones);
    if (path === "/api/triggers/preview" && method === "POST") {
      // Any expression is fine here; the timezone is checked as the server checks it.
      if (!this.timezones.includes(body.timezone)) {
        return fail(422, `Unknown timezone: '${body.timezone}'`);
      }
      return reply(200, {
        description: `Runs on "${body.cron}"`,
        next_runs: ["2026-10-15T13:00:00Z", "2026-10-16T13:00:00Z", "2026-10-17T13:00:00Z"],
      });
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
      // The server checks the graph first; the name only clashes when the save commits.
      const error = this.check(body);
      if (error) return fail(422, error);
      if (this.live().some((w) => w.name === body.name)) {
        return fail(409, "You already have a workflow with that name");
      }
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
      const rest = m[2] ?? "";
      // A deleted workflow's versions stay readable: runs and other versions still pin them.
      const pinned = method === "GET" && /^\/versions\/\d+$/.test(rest);
      if (!workflow || (workflow.deleted && !pinned)) return fail(404, "Not Found");
      if (rest === "" && method === "GET") return reply(200, this.workflowOut(workflow));
      if (rest === "" && method === "PUT") {
        // In the server's order: the version, then the graph, then (as the save commits) the name.
        const now = workflow.versions.length;
        if (body.base_version != null && body.base_version !== now) {
          return fail(
            409,
            `This workflow has changed since you started editing (now v${now}). Reload to see the changes.`,
          );
        }
        const error = this.check(body, workflow);
        if (error) return fail(422, error);
        if (this.live().some((w) => w.name === body.name && w !== workflow)) {
          return fail(409, "You already have a workflow with that name");
        }
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
      if (rest === "/runs" && method === "GET") {
        return reply(
          200,
          [...this.runs.values()]
            .filter((r) => r.workflow_id === workflow.id)
            .reverse()
            .map((r) => this.runSummary(r)),
        );
      }
      if (rest === "/runs" && method === "POST") {
        if (!body?.input?.trim()) return fail(422, "input: String should match pattern");
        if (this.current(workflow).nodes.length === 0) {
          return fail(422, "This workflow has no nodes to run");
        }
        const run = this.addRun(workflow, {
          status: "queued",
          input: body.input,
          started_at: null,
        });
        return reply(201, this.runSummary(run));
      }
      if ((m = rest.match(/^\/runs\/(\d+)$/))) {
        const run = this.runs.get(Number(m[1]));
        if (!run || run.workflow_id !== workflow.id) return fail(404, "Not Found");
        if (method === "DELETE") {
          this.runs.delete(run.id);
          return reply(204);
        }
        return reply(200, {
          ...this.runSummary(run),
          version: this.versionOut(this.workflowVersions.get(run.workflow_version_id)!),
          steps: run.steps,
        });
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

  private runSummary({ steps: _steps, ...run }: RunRow) {
    return {
      ...run,
      workflow_version: this.workflowVersions.get(run.workflow_version_id)!.version,
    };
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
