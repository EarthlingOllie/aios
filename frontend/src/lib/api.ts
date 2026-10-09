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

export type SkillSummary = {
  id: number;
  name: string;
  description: string;
  updated_at: string;
};

export type Skill = SkillSummary & {
  content: string;
  created_at: string;
  agents: { id: number; name: string }[];
};

export type SkillInput = Pick<Skill, "name" | "description" | "content">;

export type AgentSummary = {
  id: number;
  name: string;
  description: string;
  skill_count: number;
  run_count: number;
  updated_at: string;
};

export type Agent = {
  id: number;
  name: string;
  description: string;
  base_prompt: string;
  skills: SkillSummary[];
  /** The instructions a run sends to the model: base prompt plus skills. */
  instructions: string;
  created_at: string;
  updated_at: string;
};

export type AgentInput = Pick<Agent, "name" | "description" | "base_prompt"> & {
  skill_ids: number[];
};

export type RunSummary = {
  id: number;
  agent_id: number;
  title: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
};

/** A Pydantic AI `ModelRequest` / `ModelResponse`, as stored for the run. */
export type ModelMessage = {
  kind: "request" | "response";
  parts: ({ part_kind: string } & Record<string, unknown>)[];
  timestamp?: string;
  instructions?: string | null;
  model_name?: string | null;
  usage?: { input_tokens?: number; output_tokens?: number };
};

export type Run = RunSummary & {
  ui_messages: UIMessage[];
  messages: ModelMessage[];
};

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

  listSkills: () => request<SkillSummary[]>("/api/skills"),
  getSkill: (id: number) => request<Skill>(`/api/skills/${id}`),
  createSkill: (body: SkillInput) => request<Skill>("/api/skills", json("POST", body)),
  updateSkill: (id: number, body: SkillInput) =>
    request<Skill>(`/api/skills/${id}`, json("PUT", body)),
  deleteSkill: (id: number) => request<void>(`/api/skills/${id}`, { method: "DELETE" }),
  listAgents: () => request<AgentSummary[]>("/api/agents"),
  getAgent: (id: number) => request<Agent>(`/api/agents/${id}`),
  createAgent: (body: AgentInput) => request<Agent>("/api/agents", json("POST", body)),
  updateAgent: (id: number, body: AgentInput) =>
    request<Agent>(`/api/agents/${id}`, json("PUT", body)),
  deleteAgent: (id: number) => request<void>(`/api/agents/${id}`, { method: "DELETE" }),
  listRuns: (agentId: number) => request<RunSummary[]>(`/api/agents/${agentId}/runs`),
  getRun: (agentId: number, runId: number) =>
    request<Run>(`/api/agents/${agentId}/runs/${runId}`),
  createRun: (agentId: number) =>
    request<RunSummary>(`/api/agents/${agentId}/runs`, { method: "POST" }),
  deleteRun: (agentId: number, runId: number) =>
    request<void>(`/api/agents/${agentId}/runs/${runId}`, { method: "DELETE" }),
};
