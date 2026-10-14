import type { UIMessage } from "ai";

export type User = {
  id: number;
  login: string;
  name: string | null;
  avatar_url: string | null;
};

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export type DocumentInfo = {
  id: number;
  filename: string;
  status: DocumentStatus;
  error: string | null;
  chunk_count: number;
  created_at: string;
};

/** A passage found by the hybrid (semantic + keyword) document search. */
export type SearchHit = {
  chunk_id: number;
  document_id: number;
  filename: string;
  text: string;
  meta: { headings?: string[]; pages?: number[] };
  /** Reciprocal Rank Fusion score: higher is better. */
  score: number;
  /** The passage's rank in each branch, or null if that branch didn't find it. */
  semantic_rank: number | null;
  lexical_rank: number | null;
};

export type SkillSummary = {
  id: number;
  name: string;
  description: string;
  version: number;
  version_id: number;
  updated_at: string;
};

export type SkillVersionSummary = {
  id: number;
  version: number;
  note: string | null;
  created_at: string;
};

export type SkillVersion = SkillVersionSummary & {
  /** The skill's name when this version was saved. */
  name: string;
  description: string;
  content: string;
};

export type Skill = {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
  current_version: SkillVersion;
  /** Agents whose current version uses some version of this skill. */
  agents: { agent_id: number; agent_name: string; agent_version: number; skill_version: number }[];
};

export type SkillInput = { name: string; description: string; content: string; note?: string };

export type AgentSkill = {
  skill_id: number;
  skill_version_id: number;
  name: string;
  description: string;
  version: number;
  /** The skill's current version; null if the skill has been deleted. */
  latest_version: number | null;
  latest_version_id: number | null;
};

/** A JSON schema property, as Pydantic emits them for tool settings. */
export type SchemaProperty = {
  type?: "string" | "integer" | "number" | "boolean" | "array";
  title?: string;
  description?: string;
  minimum?: number;
  maximum?: number;
  items?: SchemaProperty;
};

/** A tool in the library, which can be given to agents. */
export type ToolInfo = {
  key: string;
  name: string;
  description: string;
  config_schema: { properties?: Record<string, SchemaProperty> };
  /** The settings a newly added tool starts with. */
  default_config: Record<string, unknown>;
};

export type AgentTool = {
  key: string;
  name: string;
  description: string;
  config: Record<string, unknown>;
  /** False if the tool has since been removed from the library. */
  available: boolean;
};

export type AgentToolInput = { key: string; config: Record<string, unknown> };

export type AgentVersionSummary = {
  id: number;
  version: number;
  note: string | null;
  created_at: string;
  skill_count: number;
  tool_count: number;
  run_count: number;
};

export type AgentVersion = {
  id: number;
  version: number;
  note: string | null;
  created_at: string;
  base_prompt: string;
  model: string | null;
  /** The exact instructions this version sends to the model. */
  instructions: string;
  skills: AgentSkill[];
  tools: AgentTool[];
};

export type AgentSummary = {
  id: number;
  name: string;
  description: string;
  version: number;
  skill_count: number;
  tool_count: number;
  run_count: number;
  updated_at: string;
};

export type Agent = {
  id: number;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  current_version: AgentVersion;
};

export type AgentInput = {
  name: string;
  description: string;
  base_prompt: string;
  /** Specific skill versions, in order. */
  skill_version_ids: number[];
  /** Tools from the library, in order, each with its settings. */
  tools: AgentToolInput[];
  note?: string;
};

export type RunSummary = {
  id: number;
  agent_id: number;
  agent_version_id: number;
  agent_version: number;
  title: string | null;
  message_count: number;
  /** The trigger (e.g. a schedule) that started the run, if one did and it still exists. */
  trigger_id: number | null;
  created_at: string;
  updated_at: string;
};

/** A content block of a model response (Anthropic's format), e.g. text, thinking or web search. */
export type ContentBlock = { type: string } & Record<string, unknown>;

/** A LangChain message, as stored in the run's LangGraph thread. */
export type ModelMessage = {
  type: "human" | "ai" | "tool" | "system";
  id?: string | null;
  content: string | (string | ContentBlock)[];
  /** AI messages: the tool calls the agent runs. */
  tool_calls?: { id: string; name: string; args: unknown }[];
  /** Tool messages: the call they answer, and whether it failed. */
  tool_call_id?: string;
  name?: string | null;
  status?: "success" | "error";
  usage_metadata?: { input_tokens?: number; output_tokens?: number } | null;
  response_metadata?: { model_name?: string; stop_reason?: string };
};

export type Run = RunSummary & {
  ui_messages: UIMessage[];
  messages: ModelMessage[];
  /** The pinned version's instructions: the system prompt of every model call. */
  instructions: string;
};

export type WorkflowSummary = {
  id: number;
  name: string;
  description: string;
  version: number;
  version_id: number;
  node_count: number;
  updated_at: string;
};

export type WorkflowNode = {
  key: string;
  kind: "agent" | "workflow";
  version_id: number;
  /** The agent or workflow the pinned version belongs to. */
  target_id: number;
  name: string;
  version: number;
  latest_version: number | null;
  latest_version_id: number | null;
};

export type Workflow = {
  id: number;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  current_version: {
    id: number;
    version: number;
    note: string | null;
    created_at: string;
    nodes: WorkflowNode[];
    edges: { source: string; target: string }[];
  };
};

export type TriggerTargetKind = "agent" | "workflow";
export type TriggerStatus = "running" | "succeeded" | "failed";

/** Starts an agent or a workflow by itself. Schedules (cron) are the only kind so far. */
export type Trigger = {
  id: number;
  kind: "schedule";
  target_kind: TriggerTargetKind;
  target_id: number;
  target_name: string;
  enabled: boolean;
  /** The message an agent's run starts with. */
  input: string;
  cron: string;
  /** The cron expression in plain English. */
  description: string;
  timezone: string;
  /** Null while disabled. */
  next_run_at: string | null;
  last_fired_at: string | null;
  last_status: TriggerStatus | null;
  last_error: string | null;
  last_run_id: number | null;
  created_at: string;
  updated_at: string;
};

export type TriggerInput = {
  kind: "schedule";
  target_kind: TriggerTargetKind;
  target_id: number;
  enabled: boolean;
  input: string;
  cron: string;
  timezone: string;
};

export type SchedulePreview = { description: string; next_runs: string[] };

// Same-origin requests: the browser attaches the session cookie automatically.
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", ...init });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return (res.status === 204 ? undefined : await res.json()) as T;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// ApiError.message is the raw response body (FastAPI JSON, or HTML from a proxy),
// so prefer a short status-based reason over echoing it.
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "session expired, sign in again";
    if (err.status === 413) return "too large";
    try {
      const detail = JSON.parse(err.message)?.detail;
      if (typeof detail === "string") return detail;
    } catch {
      // not JSON
    }
    return `request failed (${err.status})`;
  }
  return "network error";
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  me: () => request<User>("/api/me"),
  logout: () => request<void>("/logout", { method: "POST" }),
  listDocuments: () => request<DocumentInfo[]>("/api/documents"),
  deleteDocument: (id: number) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),
  uploadDocument: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<DocumentInfo>("/api/documents", { method: "POST", body });
  },
  search: (q: string, limit: number) =>
    request<SearchHit[]>(`/api/search?${new URLSearchParams({ q, limit: String(limit) })}`),

  listSkills: () => request<SkillSummary[]>("/api/skills"),
  getSkill: (id: number) => request<Skill>(`/api/skills/${id}`),
  createSkill: (body: SkillInput) => request<Skill>("/api/skills", json("POST", body)),
  updateSkill: (id: number, body: SkillInput) =>
    request<Skill>(`/api/skills/${id}`, json("PUT", body)),
  deleteSkill: (id: number) => request<void>(`/api/skills/${id}`, { method: "DELETE" }),
  listSkillVersions: (id: number) =>
    request<SkillVersionSummary[]>(`/api/skills/${id}/versions`),
  getSkillVersion: (id: number, version: number) =>
    request<SkillVersion>(`/api/skills/${id}/versions/${version}`),
  restoreSkillVersion: (id: number, version: number) =>
    request<Skill>(`/api/skills/${id}/versions/${version}/restore`, { method: "POST" }),
  listTools: () => request<ToolInfo[]>("/api/tools"),
  listAgents: () => request<AgentSummary[]>("/api/agents"),
  getAgent: (id: number) => request<Agent>(`/api/agents/${id}`),
  createAgent: (body: AgentInput) => request<Agent>("/api/agents", json("POST", body)),
  updateAgent: (id: number, body: AgentInput) =>
    request<Agent>(`/api/agents/${id}`, json("PUT", body)),
  deleteAgent: (id: number) => request<void>(`/api/agents/${id}`, { method: "DELETE" }),
  listAgentVersions: (id: number) =>
    request<AgentVersionSummary[]>(`/api/agents/${id}/versions`),
  getAgentVersion: (id: number, version: number) =>
    request<AgentVersion>(`/api/agents/${id}/versions/${version}`),
  restoreAgentVersion: (id: number, version: number) =>
    request<Agent>(`/api/agents/${id}/versions/${version}/restore`, { method: "POST" }),
  listRuns: (agentId: number) => request<RunSummary[]>(`/api/agents/${agentId}/runs`),
  getRun: (agentId: number, runId: number) =>
    request<Run>(`/api/agents/${agentId}/runs/${runId}`),
  createRun: (agentId: number) =>
    request<RunSummary>(`/api/agents/${agentId}/runs`, { method: "POST" }),
  deleteRun: (agentId: number, runId: number) =>
    request<void>(`/api/agents/${agentId}/runs/${runId}`, { method: "DELETE" }),

  listWorkflows: () => request<WorkflowSummary[]>("/api/workflows"),
  getWorkflow: (id: number) => request<Workflow>(`/api/workflows/${id}`),
  deleteWorkflow: (id: number) => request<void>(`/api/workflows/${id}`, { method: "DELETE" }),

  listTriggers: (target?: { kind: TriggerTargetKind; id: number }) =>
    request<Trigger[]>(
      "/api/triggers" +
        (target ? `?${new URLSearchParams({ target_kind: target.kind, target_id: String(target.id) })}` : ""),
    ),
  getTrigger: (id: number) => request<Trigger>(`/api/triggers/${id}`),
  createTrigger: (body: TriggerInput) => request<Trigger>("/api/triggers", json("POST", body)),
  updateTrigger: (id: number, body: TriggerInput) =>
    request<Trigger>(`/api/triggers/${id}`, json("PUT", body)),
  deleteTrigger: (id: number) => request<void>(`/api/triggers/${id}`, { method: "DELETE" }),
  previewSchedule: (cron: string, timezone: string) =>
    request<SchedulePreview>("/api/triggers/preview", json("POST", { cron, timezone })),
  listTimezones: () => request<string[]>("/api/triggers/timezones"),
};
