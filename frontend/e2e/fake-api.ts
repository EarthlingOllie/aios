import type { Page, Route } from "@playwright/test";

/**
 * An in-memory stand-in for the backend's agents and workflows API, enough for the browser
 * tests: it keeps state across requests, so a test can save and see the result, and follows
 * the server's rules (a changed graph is a new version, the layout isn't versioned, nodes pin
 * versions that must exist, a workflow can't contain itself, a save based on an older version
 * is refused), plus schedules' preview and timezones, and chats with their members (a chat of
 * another organisation is a 404; every chat is open: listed, read and joined by anyone in the
 * organisation, and left by anyone in it; its members' actions are a 403 for anyone who hasn't
 * joined; nobody adds or removes a person, or deletes a chat; admins archive and unarchive one,
 * which makes it read-only, a 409 for posts, joins, edits and adds; each says what `me` may do to
 * it, `actions`; names are unique within the organisation), each saying who made it and last changed
 * it, and each with an unread count of others' messages since the last one read (`chatReads`),
 * and messages (read a page at a time by id, `after` or `before` a cursor; posting joins; a
 * mention of a person who isn't a member is a 422, and of an agent or workflow the chat doesn't
 * include a 422 naming it; Jev checks each post first: `jevFlags` flags it, a 422 with the
 * reason, and with `jevUp` false it's a 503 to try again). Mentioning a workflow or an agent starts a scripted
 * invocation: its acknowledgement and card are posted with the message, and each read of the
 * chat's messages moves it on (`advanceInvocations`): briefing, then the handoff's ack and brief
 * (`handoffs`, by member id) and its run, until the run ends and the result is posted back. An
 * agent's turn takes `agentTurns` steps (by agent id; `AGENT_TURN` by default), each a read
 * adding a tool call and tokens to its run's counters. A chat made through the API has the
 * documents assistant (`documentsAssistant`) as its first agent; one added with `addChat` hasn't.
 *
 * Streams: a chat's stream and the person's are answered a batch at a time, then end, so the
 * browser opens them again after `streamRetryMs`, resuming after the last event id it has, as it
 * does when a real stream drops. Each opening of a chat's stream moves its invocations on a beat
 * too, as a read does, and sends the messages after the last event id, the invocations still
 * going, any drafts (`drafts`, or an agent turn's `drafts` as it runs), and the chat's header when
 * it has changed. The person's stream sends what changed in the organisation's chat list since it
 * was last opened. `streamsDown` answers both with a 503 and a retry hint (Redis down);
 * `streamGone` closes them with `gone` and that reason.
 */

type Kind = "agent" | "workflow" | "approval";
type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
/** What a node declares, if anything: its schemas and its input template. */
type NodeIn = {
  key: string;
  kind: Kind;
  /** Null for an approval node, which runs nothing. */
  version_id: number | null;
  input_schema?: Json | null;
  output_schema?: Json | null;
  input_template?: Json | null;
  /** An approval node's. */
  prompt_template?: string | null;
  approver_role?: "editor" | "admin" | null;
  expiry_days?: number | null;
};
/** `condition`: a JSONLogic condition, if the edge has one. */
type Edge = { source: string; target: string; condition?: Json | null };
/** The JSONLogic operators conditions may use, as the server allows them. */
const CONDITION_OPERATORS = new Set([
  "var", "==", "!=", "===", "!==", "<", "<=", ">", ">=", "!", "!!", "and", "or", "if", "in", "missing",
  "missing_some",
]);
type Position = { x: number; y: number };

type AgentRow = {
  id: number;
  name: string;
  description: string;
  base_prompt: string;
  versions: number[];
  /** The keys of the tools its current version has (`addToolUse`). */
  tools: string[];
  /** The members who made it and last changed it (`me` unless a test says otherwise). */
  created_by: ChatUser | null;
  updated_by: ChatUser | null;
  /** When it was archived, and by whom: both null while it isn't. */
  archived_at: string | null;
  archived_by: ChatUser | null;
  /** Why its versions can't run (an import's missing tool, say); null or unset if they can. */
  unrunnable?: string | null;
  /** Its versions' tools with their settings, by key (none if unset). */
  toolSettings?: Record<string, object>;
  /** Who may mention it ("everyone" if unset), and its own caps. */
  invocable_by?: InvocableBy;
  limits?: Partial<Limits>;
};
type SkillVersionRow = {
  id: number;
  version: number;
  note: string | null;
  name: string;
  description: string;
  content: string;
};
type SkillRow = {
  id: number;
  name: string;
  versions: SkillVersionRow[];
  /** The agents whose current version uses the skill, and the version of it each pins. */
  usedBy: { agent: AgentRow; version: number }[];
  /** When it was last changed, if a test sets it; NOW otherwise. */
  updated_at?: string;
  /** When it was archived, and by whom: both null while it isn't. */
  archived_at: string | null;
  archived_by: ChatUser | null;
};
/** What a run records about how it went; all null for a run from before it was kept. */
export type AgentRunCounters = {
  status: "running" | "succeeded" | "failed" | null;
  started_at: string | null;
  finished_at: string | null;
  tool_call_count: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
};
const NOT_TRACKED: AgentRunCounters = {
  status: null,
  started_at: null,
  finished_at: null,
  tool_call_count: null,
  input_tokens: null,
  output_tokens: null,
};
type AgentRunRow = AgentRunCounters & {
  id: number;
  agent_id: number;
  agent_version_id: number;
  title: string | null;
  message_count: number;
  /** Who started it (null for a schedule's run), and the schedule that did. */
  created_by: ChatUser | null;
  trigger_id: number | null;
  /** The chat it's the agent's conversation run in (one per agent and chat), if it is one. */
  chat_id?: number | null;
  /** Its conversation, as AI SDK UIMessages. */
  ui_messages: unknown[];
  /** Its turns, oldest first. */
  turns: TurnRow[];
};

/** Something an agent remembered, as its Memory tab lists it (`aios.api.agents.memories`). */
export type MemoryRow = {
  id: number;
  agent_id: number;
  content: string;
  agent_run_id: number | null;
  run_title: string | null;
  turn_id: number | null;
  chat_id: number | null;
  chat_name: string | null;
  created_at: string;
  updated_at: string;
  updated_by: ChatUser | null;
};

export type TurnRow = {
  id: number;
  invocation_id: number | null;
  agent_version_id: number;
  status: "running" | "succeeded" | "failed";
  error: string | null;
  started_at: string;
  finished_at: string | null;
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  /** What the run was told about tools left out of the turn (none if unset). */
  notices?: string[];
};

/** One of a connector's tools, as GET /connectors/:id/tools answers it. */
export type ConnectorToolRow = {
  name: string;
  description: string;
  input_schema: object;
  read_only: boolean;
};

export type SchemaChangeRow = {
  id: string;
  /** schema_change unless set: a workflow approval step's is workflow_step. */
  kind?: "schema_change" | "workflow_step";
  /** A workflow approval step's: its input and its step. */
  input?: Json | null;
  step?: { workflow_id: number; workflow_name: string; run_id: number; step_id: number; path: string[] } | null;
  expires_at?: string | null;
  decided_by?: { id: number; name: string; former_member: boolean } | null;
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
  /** Whether the signed-in user may decide it (any member of the store's organisation), while
   * it's pending. */
  decider: boolean;
  /** What approving it takes typed, if it drops anything. */
  confirm_with: string | null;
  /** The least role that may decide it (the approval rules): an editor's unless set. */
  needs_role: Role;
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
  /** If set, a restore is accepted but never happens (the worker gave up without a word). */
  stallRestore?: boolean;
};
export type NotificationRow = {
  id: string;
  /** The organisation it's from: the fake's own unless a test says otherwise. */
  organisation: { id: number; slug: string; name: string };
  kind: string;
  /** What it's about, never what it says (`approval_id` for an approval, `chat_id` and
   * `message_id` for a mention, reply or chat's failed run). */
  ref: Record<string, string | number>;
  /** What it says, for a kind other than an approval in the fake's organisation (whose title and
   * detail are read from its request, as the API reads them from the store). */
  title: string | null;
  detail: string | null;
  created_at: string;
  read_at: string | null;
  /** Whether it's the signed-in user's; the API never shows them another user's. */
  mine: boolean;
};
type WorkflowVersionRow = {
  id: number;
  workflow_id: number;
  version: number;
  note: string | null;
  created_at: string;
  nodes: NodeIn[];
  edges: Edge[];
  input_schema?: Json | null;
  output_schema?: Json | null;
};
type WorkflowRow = {
  id: number;
  name: string;
  description: string;
  layout: Record<string, Position>;
  versions: number[];
  /** When it was archived, and by whom: both null while it isn't. */
  archived_at: string | null;
  archived_by: ChatUser | null;
  created_at: string;
  updated_at: string;
  /** The members who made it and last changed it (`me` unless a test says otherwise). */
  created_by: ChatUser | null;
  updated_by: ChatUser | null;
  /** Who may mention it ("everyone" if unset), and its own caps. */
  invocable_by?: InvocableBy;
  limits?: Partial<Limits>;
};

type InvocableBy = "everyone" | "editors" | "admins";
type LimitName = "turn_model_calls" | "turn_tokens" | "turn_cost" | "run_model_calls" | "run_cost";
type Limits = Record<LimitName, number | null>;
const TURN_LIMITS: LimitName[] = ["turn_model_calls", "turn_tokens", "turn_cost"];
const RUN_LIMITS: LimitName[] = ["run_model_calls", "run_cost"];
const LIMIT_LABELS: Record<LimitName, string> = {
  turn_model_calls: "model calls per turn",
  turn_tokens: "tokens per turn",
  turn_cost: "cost per turn",
  run_model_calls: "model calls per workflow run",
  run_cost: "cost per workflow run",
};
/** The deployment's caps, as the API's settings default them. */
export const DEFAULT_LIMITS: Limits = {
  turn_model_calls: 200,
  turn_tokens: 5_000_000,
  turn_cost: 25,
  run_model_calls: 2000,
  run_cost: 100,
};
/** Usage totals by one group, as GET /api/usage answers them. */
export type UsageTotalRow = {
  key: number | string | null;
  name: string | null;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  embedded_texts: number;
  web_searches: number;
  tool_calls: number;
  sandbox_seconds: number;
  cost: number | null;
  unpriced: number;
};

/** A source: what brings content into the store. The store starts with Uploads. */
export type SourceRow = {
  id: number;
  kind: "upload" | "url" | "webhook" | "email" | "connector";
  name: string;
  status: "active" | "paused" | "failing" | "needs_reauth" | "removing";
  /** healthy, delayed, failing or needs_reconnecting; healthy unless set. */
  health?: "healthy" | "delayed" | "failing" | "needs_reconnecting";
  provider?: string | null;
  interval_seconds?: number | null;
  item_count?: number;
  /** Its syncs, newest first. */
  syncs?: SyncRow[];
  created_at: string;
  /** A URL source's page; an email source's allowed senders; a webhook source's endpoint. */
  url?: string;
  allowed_senders?: string[];
  webhook_url?: string;
};

/** Something a connector can sync; `restricted` at its source. */
export type ScopeItemRow = { id: string; name: string; restricted: boolean };

/** A connector, as GET /connectors answers it (its source is filled in from `sources`). */
export type ConnectorRow = {
  id: number;
  provider: string;
  name: string;
  status: "active" | "needs_reauth" | "removing";
  unavailable: string | null;
  scope: ScopeItemRow[];
  source_id: number | null;
  created_at: string;
};

/** What an admin must accept before syncing something restricted, as the API says it. */
export const RESTRICTED_WARNING =
  "Some of what you picked is restricted at its source. Once it's synced, everyone in the organisation will be able to search it.";

/** One sync of a source. */
export type SyncRow = {
  id: number;
  kind: "incremental" | "full" | "manual";
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  added: number;
  updated: number;
  removed: number;
  skipped: number;
  dropped: number;
  awaiting_check: number;
  storage_full: boolean;
  error: string | null;
};

/** A synced message item Jev flagged, on the flagged list. */
export type FlaggedItemRow = {
  id: number;
  title: string;
  url: string | null;
  source_id: number;
  source_name: string;
  reason: string | null;
  checked_at: string | null;
  created_at: string;
  released?: boolean;
};
/** An item of the search index (GET /api/search), without its ranking, which the fake makes up. */
export type SearchItemRow = {
  id: number;
  kind: "document_chunk" | "chat_message" | "run_message" | "brief" | "source_item";
  text: string;
  created_at: string;
  ref: { headings?: string[]; pages?: number[]; role?: "user" | "assistant" };
  document_id?: number | null;
  filename?: string | null;
  chat_id?: number | null;
  chat_name?: string | null;
  message_id?: number | null;
  agent_id?: number | null;
  agent_name?: string | null;
  agent_run_id?: number | null;
  run_title?: string | null;
};

export type DocumentRow = {
  id: number;
  filename: string;
  status: "pending" | "processing" | "ready" | "failed";
  error: string | null;
  /** Jev's verdict: only a clean or released document is found by search. */
  verdict: "awaiting_check" | "clean" | "flagged" | "released";
  /** Why Jev flagged it. */
  verdict_reason: string | null;
  chunk_count: number;
  size_bytes: number;
  /** The member who uploaded it; null for one from before uploads were recorded. */
  uploaded_by: ChatUser | null;
  /** The source it belongs to: Uploads unless it's set. */
  source: Pick<SourceRow, "id" | "kind" | "name">;
  /** A synced document's place at its source. */
  url?: string | null;
  created_at: string;
};
export type TableColumnRow = {
  name: string;
  type: string;
  nullable: boolean;
  description: string | null;
  embedding: boolean;
};
/** A `data` table, with its values as the server sends them (as text, an embedding as its type). */
export type TableRow = {
  name: string;
  description: string | null;
  columns: TableColumnRow[];
  rows: (string | null)[][];
  /** Sent as the planner's estimate rather than a count. */
  estimated?: boolean;
  /** The row count sent, if not how many `rows` there are (e.g. an estimate of millions). */
  rowCount?: number;
  /** The agent that owns it (by id): unset or null, none. */
  owner?: number | null;
  /** The agents it's shared with, by id. */
  shares?: number[];
};
/** As the server cuts values in a page of rows (`PREVIEW_CHARS`). */
const PREVIEW_CHARS = 200;
type RunStatus = "queued" | "running" | "waiting" | "succeeded" | "failed";
type StepStatus = "running" | "waiting" | "succeeded" | "failed" | "skipped";
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
  waiting_on_request_ids: string[];
  /** The approval it waits on, or an approval node's once decided. */
  approval_id?: string | null;
  started_at: string;
  finished_at: string | null;
  input_json?: Json | null;
  output_json?: Json | null;
  conditions?: Record<string, boolean> | null;
};
export type StalePinRow = {
  path: string[];
  kind: Kind;
  name: string;
  pinned_version: number;
  current_version: number | null;
};
type UsedByTriggerRow = {
  id: number;
  enabled: boolean;
  cron: string;
  description: string;
  timezone: string;
  next_run_at: string | null;
};

/** The workflows and schedules `GET /api/agents/{id}/used-by` lists for an agent. */
export type AgentUsedByRow = {
  workflows: { id: number; name: string; version: number; direct: boolean; triggers: UsedByTriggerRow[] }[];
  triggers: UsedByTriggerRow[];
};

export type RunRow = {
  id: number;
  workflow_id: number;
  workflow_version_id: number;
  status: RunStatus;
  input: string;
  output: string | null;
  error: string | null;
  input_json?: Json | null;
  output_json?: Json | null;
  output_note?: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  waiting_since: string | null;
  waited_seconds: number;
  /** Who started it (null for a schedule's run), and the schedule that did. */
  created_by: ChatUser | null;
  trigger_id: number | null;
  steps: StepRow[];
  stale_pins: StalePinRow[];
  /** For a run an agent started (`start_workflow`): that agent, and its run. */
  started_by_agent?: { agent_id: number; name: string; run_id: number | null } | null;
};

/** Someone who can sign in, as the API keeps them (the password in the clear: it's a fake). */
export type Account = { id: number; first_name: string; last_name: string; email: string; password: string };
const accountOut = ({ password: _password, ...user }: Account) => user;

/** A 422 for one field, as the API answers it: [status, body]. */
const invalid = (field: string, msg: string): [number, unknown] => [422, { detail: [{ loc: ["body", field], msg }] }];
const isEmail = (email: string) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email);

/** Why a new password (in `field`) is refused, as the API says it, or undefined when it isn't. */
function newPasswordInvalid(field: string, password: string, confirmation: unknown, confirmField: string) {
  if (password.length < 12) return invalid(field, "Password must be at least 12 characters");
  if (password.length > 256) return invalid(field, "Password must be at most 256 characters");
  if (confirmation !== password) return invalid(confirmField, "Passwords don't match");
  return undefined;
}

/** A user as chats show them (an `Account` is one). */
// `former_member`: they've left the organisation (the API says so from its store's people mirror).
export type ChatUser = Pick<Account, "id" | "first_name" | "last_name"> & { former_member?: boolean };
export type ChatMemberRow =
  | {
      id: number;
      kind: "user";
      user: ChatUser;
      added_at: string;
      /** The last message they've seen (`POST /api/chats/{id}/read`); null for none yet. */
      last_read: number | null;
    }
  /** An agent or workflow of `agents` or `workflows`, by id. */
  | { id: number; kind: Kind; target_id: number; added_at: string };
type ChatUserMemberRow = Extract<ChatMemberRow, { kind: "user" }>;
export type ChatMention = { kind: "user" | Kind; id: number; name: string };
type ChatMemberOut = {
  id: number;
  kind: "user" | Kind;
  target_id: number;
  name: string;
  added_at: string;
  archived: boolean;
};
export type Role = "admin" | "editor" | "participant";

/** An entry of the audit log, as GET /audit-log answers it. */
export type AuditEntryRow = {
  id: number;
  at: string;
  actor: {
    kind: "person" | "agent" | "system";
    user: Pick<Account, "id" | "first_name" | "last_name"> | { id: number; name: string } | null;
    agent: { id: number; name: string | null } | null;
    run_id: number | null;
  };
  action: string;
  target_kind: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
};

/** Each kind of schema change's least role by default (`aios.approval_rules`). */
const DEFAULT_RULES: Record<string, Role> = {
  create_table: "editor",
  add_column: "editor",
  create_index: "editor",
  drop_table: "admin",
  drop_column: "admin",
  restore: "admin",
  purge: "admin",
  change_ownership: "admin",
};

const RULE_LABELS: Record<string, string> = {
  create_table: "Create a table",
  add_column: "Add a column",
  create_index: "Create an index",
  drop_table: "Drop a table",
  drop_column: "Drop a column",
  restore: "Restore a dropped table or column",
  purge: "Purge a dropped table or column",
  change_ownership: "Change a table's owner",
};
/** A member, with their role: the creator's is admin and anyone else's editor unless it's set. */
export type OrganisationMemberRow = { user: ChatUser; joined_at: string; invite_id: string | null; role?: Role };
/** An open invitation link, with the token the fake keeps (the API keeps only its hash), and the
 * role it grants (editor unless it's set). */
export type InviteRow = {
  id: string;
  creator: ChatUser | null;
  created_at: string;
  expires_at: string;
  token: string;
  role?: Role;
};
export type InvitationRow = { organisation: { id: number; name: string }; expires_at: string; role?: Role };
export type OrganisationRow = {
  id: number;
  name: string;
  /** What links name it by (`/o/<slug>/`); a rename leaves it unchanged. */
  slug: string;
  creator: ChatUser | null;
  members: OrganisationMemberRow[];
  created_at: string;
  /** Its scheduled deletion, if any (`POST /api/organisation/deletion`). */
  deletion?: OrganisationDeletionRow | null;
};
export type OrganisationDeletionRow = { scheduled_at: string; scheduled_by: ChatUser | null; deletes_at: string };

export type ChatRow = {
  id: number;
  /** The organisation it belongs to: `organisation`'s unless a test says otherwise. */
  organisation_id: number;
  created_by: ChatUser | null;
  updated_by: ChatUser | null;
  name: string;
  description: string;
  members: ChatMemberRow[];
  /** Oldest first. */
  messages: ChatMessageRow[];
  created_at: string;
  updated_at: string;
  /** When an admin archived it (read-only), and who; null while it isn't. */
  archived_at?: string | null;
  archived_by?: ChatUser | null;
};
/** A message's author: a user, or an agent or workflow (by id and name). */
export type ChatAuthor = ChatUser | { kind: Kind; id: number; name: string };
export type ChatMessageRow = {
  id: number;
  author: ChatAuthor;
  kind: "text" | "run_card";
  body: string;
  mentions: ChatMention[];
  invocation_id: number | null;
  created_at: string;
};
type InvocationStatus = "queued" | "briefing" | "running" | "handing_back" | "succeeded" | "failed" | "refused";
/** An agent's or workflow's run that a mention started, or a schedule's whose result was
 * delivered to the chat (`delivered`). */
export type InvocationRow = {
  id: number;
  chat_id: number;
  message_id: number | null;
  delivered?: boolean;
  requested_by: number;
  target_kind: Kind;
  target_id: number;
  status: InvocationStatus;
  error: string | null;
  /** Why the mention guards refused it. */
  refusal_reason?: string | null;
  /** What the handoff told its run of the chat; null until it has. */
  brief: string | null;
  /** The handback: the full output it summarised, the summary posted, and why summarising failed. */
  full_output?: string | null;
  summary?: string | null;
  handback_error?: string | null;
  delivery_error?: string | null;
  /** The run it started; gone from `runs` (or `agentRuns`) once it's deleted. */
  run_id: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

/** Whether a mention's result is handed back before it's posted, as the API decides it. */
function handsBack(invocation: InvocationRow, body: string): boolean {
  if (invocation.delivered) return false;
  if (invocation.target_kind === "workflow") return body.trim() !== "";
  const trimmed = body.trim();
  if (trimmed.length > HANDBACK_THRESHOLD) return true;
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return false;
  try {
    JSON.parse(trimmed);
    return true;
  } catch {
    return false;
  }
}

/** How the API names a user (the backend's `User.full_name`): "First Last". */
function fullName(user: ChatUser): string {
  return `${user.first_name} ${user.last_name}`;
}

/**
 * A user anywhere in a response, as the API names them: by their full name, as `name` (who made
 * or changed something, a member, an invitation's creator), and never with their password.
 */
function asTheApiNamesThem(_key: string, value: unknown) {
  if (value && typeof value === "object" && "first_name" in value && "last_name" in value && !("name" in value)) {
    const { password: _password, ...user } = value as Account;
    return { ...user, name: fullName(user) };
  }
  return value;
}

/** Whether a message's author is a user (rather than an agent or a workflow). */
const isUser = (author: ChatAuthor): author is ChatUser => "first_name" in author;

/** A chat's members (or what can be mentioned in it) grouped by kind, as the API groups them. */
function byKind<T extends { kind: ChatMention["kind"] }>(members: T[]) {
  const of = (kind: ChatMention["kind"]) => members.filter((m) => m.kind === kind);
  return { users: of("user"), agents: of("agent"), workflows: of("workflow") };
}

/** How many messages a read returns at most, as the API's `MESSAGE_PAGE`. */
export const MESSAGE_PAGE = 50;

/** The slug of the fake's organisation, as a new fake has it: its pages are under `/o/tester/`. */
export const ORGANISATION_SLUG = "tester";

/** A page's path in the fake's organisation, as the app's links have it: `/o/tester/agents`. */
export function inOrg(path: string): string {
  return `/o/${ORGANISATION_SLUG}${path}`;
}

/** The id of the fake's organisation, as a new fake has it: what its API paths name it by. */
export const ORGANISATION_ID = 1;

/** An API path in the fake's organisation, as the app sends it: `/api/o/1/agents`. */
export function orgApi(path: string, organisationId: number = ORGANISATION_ID): string {
  return `/api/o/${organisationId}${path}`;
}

/** A slug as the API makes one from a name (`aios.organisations.slugify`). */
function slugify(name: string): string {
  const ascii = name.normalize("NFKD").replace(/[^\x00-\x7f]/g, "");
  return ascii.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "organisation";
}

const NOW = "2026-10-14T09:00:00Z";

/** How a mentioned agent's scripted turn goes: how many reads it runs for (each a model step
 * adding a tool call and `tokensPerStep` tokens), then its reply, or the error it fails with. */
export type AgentTurn = {
  steps: number;
  tokensPerStep: number;
  reply?: string;
  error?: string;
  /** The reply so far after each step, as the chat's stream sends it while the turn runs. */
  drafts?: string[];
};
export const AGENT_TURN: AgentTurn = { steps: 2, tokensPerStep: 120 };

/** A message's text: each mention as `@name`. */
const messageText = (body: string) =>
  body.replace(/@\[((?:\\.|[^\\\]])*)\]\((?:user|agent|workflow):\d+\)/g, "@$1");

/** A run's input: the handoff's brief, a blank line, then the message's text (just the text with
 * no brief). */
const runInput = (brief: string | null, body: string) => (brief ? `${brief}\n\n${messageText(body)}` : messageText(body));

/** What a mentioned member's handoff writes: what it understood and the brief, or why it failed. */
export type Handoff = { ack: string; brief: string } | { error: string };
/** What the handback answers for a result (`handbacks`, by target id): a summary (by default the
 * output as it came), or an error, which posts the raw output with a note. */
export type Handback = { summary: string } | { error: string };
/** Agent replies longer than this are handed back (the API's `handback_threshold`). */
export const HANDBACK_THRESHOLD = 3000;
export const HANDOFF: Handoff = { ack: "On it.", brief: "" };
/** The acknowledgement's placeholder, until the handoff writes what was understood. */
const ACKNOWLEDGEMENT = "Reading the chat…";
const CHAT_NAME_TAKEN = "The organisation already has a chat with that name.";
const ARCHIVED = "This chat is archived, so it's read-only. An admin can unarchive it.";

/** An event trigger's filter as the API normalises it: lower-case strings (tables without
 * `data.`), empty fields left out; `chat_id` a single value. */
function normalisedFilter(filter: Record<string, unknown> = {}): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, values] of Object.entries(filter)) {
    if (!Array.isArray(values)) {
      if (values !== undefined && values !== null) out[key] = values;
      continue;
    }
    const kept = values
      .map((v) => (typeof v === "string" ? v.trim().toLowerCase() : v))
      .map((v) => (key === "tables" && typeof v === "string" ? v.replace(/^data\./, "") : v))
      .filter((v) => v !== "");
    if (kept.length) out[key] = kept;
  }
  return out;
}

/** What the Insights panels answer: the grouped ones by grouping. */
export type InsightsData = {
  activity: Record<string, { turns: object[]; workflow_runs: object[] }>;
  usage: Record<string, { rows: object[]; top_agents: object[]; top_tools: object[] }>;
  workflows: { slowest_workflows: object[]; slowest_nodes: object[]; failures: object[] };
  approvals: { kinds: object[] };
  chats: { chats: object[] };
  storage: {
    db_quota_bytes: number;
    files_quota_bytes: number;
    db_bytes: number | null;
    files_bytes: number | null;
    measured_at: string | null;
    samples: { measured_at: string; db_bytes: number; files_bytes: number }[];
  };
  sources: { shown: boolean; sources: object[] };
};

export function emptyInsights(): InsightsData {
  const groups = <T,>(empty: () => T) => ({ agent: empty(), workflow: empty(), cause: empty(), person: empty() });
  return {
    activity: groups(() => ({ turns: [], workflow_runs: [] })),
    usage: groups(() => ({ rows: [], top_agents: [], top_tools: [] })),
    workflows: { slowest_workflows: [], slowest_nodes: [], failures: [] },
    approvals: { kinds: [] },
    chats: { chats: [] },
    storage: {
      db_quota_bytes: 1024 ** 3,
      files_quota_bytes: 5 * 1024 ** 3,
      db_bytes: null,
      files_bytes: null,
      measured_at: null,
      samples: [],
    },
    sources: { shown: false, sources: [] },
  };
}
/** The filter fields each event type's triggers take (`aios.outbox.FILTERS`). */
const FILTER_FIELDS: Record<string, string[]> = {
  document_ingested: ["sources", "content_types"],
  table_written: ["tables", "operations"],
  schema_change_applied: ["tables"],
  chat_message: ["chat_id", "author_kinds", "mentions", "keywords"],
  webhook_received: ["sources"],
};

/** What an event trigger fires on, as the API describes it. */
const EVENT_DESCRIPTIONS: Record<string, string> = {
  document_ingested: "When a document is ingested",
  table_written: "When a data table is written",
  schema_change_applied: "When a schema change is applied",
  chat_message: "When a message is posted in a chat",
  webhook_received: "When its webhook is called",
};

export const WEBHOOK_URL = "http://localhost:5173/api/webhooks/";

export class FakeApi {
  private nextId = 100;
  agents = new Map<number, AgentRow>();
  /** The agent that's `me`'s documents assistant, once a new chat has made it. */
  assistantId: number | null = null;
  // Agent version id -> [agent id, version number].
  agentVersions = new Map<number, [number, number]>();
  // Agent version id -> note, for the versions that have one.
  agentNotes = new Map<number, string>();
  skills = new Map<number, SkillRow>();
  /** The tool library (`GET /api/tools`): empty unless a test adds to it (`addTool`). */
  tools: { key: string; name: string; description: string; config_schema: object; default_config: object }[] = [];
  workflows = new Map<number, WorkflowRow>();
  workflowVersions = new Map<number, WorkflowVersionRow>();
  runs = new Map<number, RunRow>();
  agentRuns = new Map<number, AgentRunRow>();
  /** Agents' memories, by id (`addMemory`). */
  memories = new Map<number, MemoryRow>();
  /**
   * What depends on each agent, by agent id (none if unset). As on the server, deleting one is
   * refused until each listed run is named to cancel and each workflow to deactivate; a test
   * "edits" a workflow by taking it off the list.
   */
  /** What uses each agent, as its archive confirmation lists it (none unless a test says). */
  agentUsedBy = new Map<number, AgentUsedByRow>();
  /**
   * Who can sign in. The first is signed in from the start, so a test needn't sign in unless it's
   * about signing in.
   */
  accounts: Account[] = [
    { id: 1, first_name: "Terry", last_name: "Tester", email: "tester@example.com", password: "correct horse battery" },
  ];
  /**
   * The signed-in account's id; null once signed out. Signed out, only /api/auth/*, /api/dev/*
   * and opening an invitation link answer: everything else is a 401.
   */
  signedInAs: number | null = 1;
  /** How long signing in or registering takes to answer (ms). */
  authDelay = 0;
  /** Whether the dev "sign in as" picker is on (DEV_LOGIN_ENABLED): off, /api/dev/* is a 404. */
  devUsers = false;
  /**
   * While true, signing in, registering, and the profile's current-password checks (changing the
   * email or password) answer 429, as the API's rate limits do.
   */
  authThrottled = false;
  /** The signed-in account (the first while no one is): who a chat's owner, member or author is by default. */
  get me(): Account {
    return this.accounts.find((a) => a.id === this.signedInAs) ?? this.accounts[0];
  }
  /**
   * The organisation `me` is in, as `GET /api/organisation` answers: one they created, with them
   * as its first member. `me` is its creator while `creator` is them.
   */
  organisation: OrganisationRow = {
    id: ORGANISATION_ID,
    name: "tester",
    slug: ORGANISATION_SLUG,
    creator: this.me,
    members: [{ user: this.me, joined_at: "2026-10-14T08:00:00Z", invite_id: null }],
    created_at: "2026-10-14T08:00:00Z",
  };
  /**
   * Whether `me` is a member of `organisation`: false for a person who has registered without an
   * invitation, or left it, until they create or join one. While false, `/api/me` doesn't list
   * it and its routes answer 404, as for any organisation they're not in.
   */
  member = true;
  /**
   * Other organisations `me` is in, as `/api/me` lists them, before `organisation`: joined more
   * recently unless a test puts them after it (`joinedEarlier`). Only "me" sees them; every
   * other endpoint is `organisation`'s.
   */
  otherMemberships: {
    id: number;
    name: string;
    slug: string;
    is_creator: boolean;
    role?: Role;
    deletes_at?: string | null;
  }[] = [];
  /** Whether `otherMemberships` were joined before `organisation` (listed after it). */
  joinedEarlier = false;
  /** The organisation's overrides of the deployment's caps (`PUT /api/organisation/limits`). */
  limits: Limits = { turn_model_calls: null, turn_tokens: null, turn_cost: null, run_model_calls: null, run_cost: null };
  /** Usage totals by group (`GET /api/usage`), whatever the range; and the query of each read. */
  usage: Record<string, UsageTotalRow[]> = { agent: [], workflow: [], person: [], day: [] };
  usageReads: string[] = [];
  /** The mention guards: how many agents and workflows a message may mention; how many allowed
   * mentions `me` may make (a 429 past it, retry in `mentionRetryAfter` seconds), and has made. */
  mentionsPerMessage = 5;
  mentionRate = 60;
  mentionsMade = 0;
  mentionRetryAfter = 42;
  /** How long runs and chat history are kept (`PUT /api/organisation/retention`). */
  retention: { run_days: number | null; chat_days: number | null } = { run_days: null, chat_days: null };
  /** How many days an approval waits undecided (`PUT /api/organisation/approval-expiry`). */
  approvalExpiryDays = 7;
  /** Whether Insights shows usage by person to everyone (`PUT /api/organisation/insights-people`). */
  insightsPeopleVisible = false;
  /** The budget (`GET` and `PUT /api/organisation/budget`): the monthly budget (null: none),
   * this month's spend, each role's allowance, members' own allowances and their spend, by user
   * id (`PUT .../organisation/members/{id}/allowance`). */
  budget: {
    month: string;
    budget: number | null;
    spent: number;
    role_allowances: Record<Role, number | null>;
    allowances: Record<number, number | null>;
    spends: Record<number, number>;
  } = {
    month: "2026-11",
    budget: null,
    spent: 0,
    role_allowances: { admin: null, editor: null, participant: null },
    allowances: {},
    spends: {},
  };
  /** Each Insights panel's data (`GET /api/insights/{panel}`), whatever the range; by grouping
   * for the grouped ones; and the path and query of each read. */
  insights: InsightsData = emptyInsights();
  insightsReads: string[] = [];
  /** What the insights agent answers each question with (`POST /api/insights/ask`), in turn:
   * the chart and its rows, or an error with the SQL; a status refuses the question instead (a
   * 429 past the rate, a 409 at the budget). The questions asked, in order. */
  chartAnswers: ({ title: string; sql: string; spec: Record<string, unknown>; data?: Record<string, unknown>[]; truncated?: boolean; error?: string } | { status: number; detail: string })[] = [];
  chartQuestions: string[] = [];
  /** The organisation's saved charts (`/api/insights/charts`), newest first, with their rows
   * (or why their query no longer works) by id. */
  savedCharts: {
    id: number;
    title: string;
    question: string;
    sql: string;
    spec: Record<string, unknown>;
    created_by: { id: number; name: string; former_member?: boolean } | null;
    created_at: string;
    updated_at: string;
  }[] = [];
  chartRows: Record<number, { data: Record<string, unknown>[]; truncated?: boolean } | { broken: string }> = {};
  /** How many days a scheduled deletion waits, as `ORGANISATION_DELETION_GRACE_DAYS` does. */
  deletionGraceDays = 14;
  /** The organisation's open invitation links, oldest first (made with `POST`, or seeded). */
  invites: InviteRow[] = [];
  /**
   * Invitation links into other organisations, by token, as `GET /api/invites/{token}` opens
   * them (the organisation's own open `invites` open too). Any other token is a 404.
   */
  invitations = new Map<string, InvitationRow>();
  chats = new Map<number, ChatRow>();
  /** The schedules, as `GET /api/triggers` lists them. */
  schedules: Record<string, unknown>[] = [];
  invocations = new Map<number, InvocationRow>();
  /** What each mentioned agent's or workflow's handoff writes, by its id (`HANDOFF` if unset). */
  handoffs = new Map<number, Handoff>();
  /** Workflows whose runs a mention can't start, by id, with why (as the API refuses one). */
  refuseStart = new Map<number, string>();
  /** How each mentioned agent's turn goes, by agent id (`AGENT_TURN` if unset). */
  agentTurns = new Map<number, AgentTurn>();
  /** What the handback answers for each agent's or workflow's results, by target id. */
  handbacks = new Map<number, Handback>();
  /** The timezones the server knows; it may list ones a browser's Intl doesn't (e.g. "localtime"). */
  timezones = ["America/New_York", "Europe/London", "UTC", "localtime"];
  /** The organisation's data store's status: documents and search answer 409 until it's "ready". */
  dataStoreStatus: "provisioning" | "ready" | "failed" = "ready";
  /**
   * What the store uses, as last recorded, and its caps. Uploads answer 507 at either cap; as
   * the API does, uploads and deletes record the documents' bytes again.
   */
  dataStoreUsage: {
    db_bytes: number | null;
    db_quota_bytes: number;
    files_bytes: number | null;
    files_quota_bytes: number;
    measured_at: string | null;
  } = {
    db_bytes: 8 * 1024 ** 2,
    db_quota_bytes: 1024 ** 3,
    files_bytes: 0,
    files_quota_bytes: 5 * 1024 ** 3,
    measured_at: "2026-10-15T08:55:00Z",
  };
  /** How long GET /api/data-store takes to answer (ms). */
  dataStoreDelay = 0;
  /** While true, GET /api/data-store answers 500, as if the server can't reach the store. */
  dataStoreFails = false;
  /** What the search index holds besides the ready documents' passages (each document is one
   * passage, found by its name): chat messages, past runs' messages and briefs a test adds. A
   * search finds those whose text contains its query. */
  searchItems: SearchItemRow[] = [];
  /** The query strings of GET /api/search, in order. */
  searches: string[] = [];
  /** The organisation's sources: Uploads, made with the store, and any `addSource` adds. */
  sources: SourceRow[] = [
    { id: 1, kind: "upload", name: "Uploads", status: "active", created_at: NOW },
  ];
  /** The kinds of source an admin can add (GET /api/source-kinds). */
  sourceKinds: { kind: "url" | "webhook" | "email"; unavailable: string | null }[] = [
    { kind: "url", unavailable: null },
    { kind: "webhook", unavailable: null },
    { kind: "email", unavailable: null },
  ];
  /** How many webhook secrets and email addresses have been made, so each is new. */
  private secrets = 0;
  /** The providers a source can be added with (GET /api/source-providers). */
  sourceProviders: { provider: string; unavailable: string | null }[] = [{ provider: "test", unavailable: null }];
  /** The providers a connector can be made with (GET /api/connector-providers). */
  connectorProviders: { provider: string; title: string; unavailable: string | null }[] = [
    { provider: "slack", title: "Slack", unavailable: "Slack isn't set up in this deployment: it needs its app settings." },
    {
      provider: "google_drive",
      title: "Google Drive",
      unavailable: "Google Drive isn't set up in this deployment: it needs its app settings.",
    },
    { provider: "github", title: "GitHub", unavailable: "GitHub isn't set up in this deployment: it needs its app settings." },
    { provider: "test", title: "Test", unavailable: null },
  ];
  /** The organisation's connectors. Connecting one (POST /connectors) makes it at once, as if the
   * provider had sent the browser straight back. */
  connectors: ConnectorRow[] = [];
  /** What every connector can sync (GET /connectors/:id/scope-options). */
  scopeOptions: ScopeItemRow[] = [
    { id: "general", name: "general", restricted: false },
    { id: "random", name: "random", restricted: false },
    { id: "board", name: "board", restricted: true },
  ];
  /** Each scope saved: the connector, the ids picked, and whether restricted ones were accepted. */
  scopeSaves: { id: number; items: string[]; acknowledged: boolean }[] = [];
  /** What each provider's connector offers as tools (GET /connectors/:id/tools): the test
   * provider's two, as the server's test provider serves them. */
  connectorTools: Record<string, ConnectorToolRow[]> = {
    test: [
      {
        name: "post_message",
        description: "Post a message to a channel.",
        input_schema: { type: "object", properties: { channel: { type: "string" }, text: { type: "string" } } },
        read_only: false,
      },
      {
        name: "read_channel",
        description: "Read a channel's messages.",
        input_schema: { type: "object", properties: { channel: { type: "string" } } },
        read_only: true,
      },
    ],
  };
  /** Each connector removed, and whether its content was kept. */
  connectorRemovals: { id: number; keep: boolean }[] = [];
  /** Each Sync now and Full resync asked for: the source's id, and whether it was full. */
  syncRequests: { id: number; full: boolean }[] = [];
  /** Synced message items Jev flagged. */
  flaggedItems: FlaggedItemRow[] = [];
  /** The organisation's documents, newest first. */
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
  /** Schema change requests in the organisation's store, oldest first. */
  schemaChanges: SchemaChangeRow[] = [];
  /** The organisation's audit log, newest first, as GET /audit-log answers it. */
  auditEntries: AuditEntryRow[] = [];
  /** Whether Jev answered its last health check (null: not known), as GET /jev-status answers. */
  jevUp: boolean | null = true;
  /** What Jev flags in a posted chat message: the commonest injection phrasing, as its fake does. */
  jevFlags = /ignore\s+(all\s+)?(the\s+)?(previous|prior|above|your)\s+(instructions|rules)/i;
  /** The approval rules, by kind: each kind's least role, as an admin set it. */
  approvalRules: Record<string, Role> = { ...DEFAULT_RULES };
  /** Dropped tables and columns in the organisation's store, newest first. */
  dropped: DroppedRow[] = [];
  /**
   * Notifications, oldest first. As on the server, a pending request the user may decide
   * notifies them (approval needed), and deciding it marks that read; reading a chat past a
   * message reads those about it; `mine: false` ones are another user's.
   */
  notifications: NotificationRow[] = [];
  /** The user's notifications as the person's stream last saw them (id: read), to tell it of changes. */
  private notificationsSeen: Map<string, boolean> | null = null;
  /** How many times the app asked for the user's notifications. */
  notificationLists = 0;
  /** The query of each read of a chat's messages, in order (e.g. "?after=12"). */
  messageReads: string[] = [];
  /** The query of each opening of a chat's stream, in order (e.g. "?last_event_id=12"). */
  streamReads: string[] = [];
  /** How many times the person's stream was opened. */
  personStreamReads = 0;
  /** How long the browser waits before opening a stream again once a batch has ended (ms). */
  streamRetryMs = 1000;
  /** Redis down: streams answer 503 with a retry hint. */
  streamsDown = false;
  /** Why streams close for good (`gone`), e.g. "left_organisation"; null while they don't. */
  streamGone: string | null = null;
  /** Replies being written, by invocation id. */
  drafts = new Map<number, string>();
  /** Each chat's header as its stream last sent it. */
  private headersSent = new Map<number, string>();
  /** The chat list as the person's stream last saw it, by chat id. */
  private listSeen: Map<number, string> | null = null;
  /** Every write the app sent, for assertions: [method, path, body]. */
  writes: [string, string, unknown][] = [];
  /** Each `POST /api/chats/{id}/read`, as [chat id, message id]: not a write the user made. */
  chatReads: [number, number][] = [];
  /** The store's `data` tables, by name. */
  dataTables: TableRow[] = [];
  /** While true, the tables endpoints answer 500. */
  tablesFail = false;
  /** Every page of rows the app asked for, as its query string. */
  rowReads: string[] = [];

  private id() {
    return this.nextId++;
  }

  /** The organisation as `GET /api/organisation` answers. */
  private organisationOut() {
    return {
      ...this.organisationSummary(),
      ...this.organisation,
      members: this.organisation.members.map((m) => ({ ...m, role: this.roleOf(m.user.id) })),
      deletion: this.organisation.deletion ?? null,
      retention: { ...this.retention },
      approval_expiry_days: this.approvalExpiryDays,
      insights_people_visible: this.insightsPeopleVisible,
      limits: { defaults: { ...DEFAULT_LIMITS }, overrides: { ...this.limits }, in_force: this.limitsInForce() },
    };
  }

  /** The budget as `GET /api/organisation/budget` answers: every member's spend for an admin, or
   * for anyone when usage by person is shown to everyone; else only `me`'s. */
  private budgetOut() {
    const everyone = this.roleOf(this.me.id) === "admin" || this.insightsPeopleVisible;
    const members = this.organisation.members
      .filter((m) => everyone || m.user.id === this.me.id)
      .map((m) => {
        const role = this.roleOf(m.user.id);
        const allowance = this.budget.allowances[m.user.id] ?? null;
        return {
          user: m.user,
          role,
          allowance,
          allowance_in_force: allowance ?? this.budget.role_allowances[role],
          spent: this.budget.spends[m.user.id] ?? 0,
        };
      });
    const { allowances: _a, spends: _s, ...rest } = this.budget;
    return { ...rest, role_allowances: { ...rest.role_allowances }, warning_threshold: 0.8, members, everyone };
  }

  /** The organisation's caps in force: its overrides, else the deployment's. */
  limitsInForce(): Limits {
    const out = { ...DEFAULT_LIMITS };
    for (const [k, v] of Object.entries(this.limits)) if (v !== null) out[k as LimitName] = v;
    return out;
  }

  /** An agent's or workflow's own caps (`names`), null where it follows the organisation's. */
  private ownLimits(row: { limits?: Partial<Limits> }, names: LimitName[]) {
    return Object.fromEntries(names.map((n) => [n, row.limits?.[n] ?? null]));
  }

  private limitsInForceFor(row: { limits?: Partial<Limits> }, names: LimitName[]) {
    const org = this.limitsInForce();
    return Object.fromEntries(names.map((n) => [n, row.limits?.[n] ?? org[n]]));
  }

  /** As the API sets an agent's or workflow's guards from a request: a 403's detail if a
   * non-admin sets a cap above the organisation's (one already at it may be kept). */
  private applyGuards(row: { invocable_by?: InvocableBy; limits?: Partial<Limits> }, body: Record<string, unknown>) {
    const limits = body.limits as Partial<Limits> | undefined;
    if (limits && this.roleOf(this.me.id) !== "admin") {
      const org = this.limitsInForce();
      for (const [name, value] of Object.entries(limits)) {
        const n = name as LimitName;
        if (value != null && value > org[n]! && row.limits?.[n] !== value) {
          const shown = n.endsWith("_cost") ? `£${org[n]!.toFixed(2)}` : org[n]!.toLocaleString("en-GB");
          return `Only admins can raise a limit above the organisation's (${LIMIT_LABELS[n]}: ${shown}). Ask an admin.`;
        }
      }
    }
    if (body.invocable_by) row.invocable_by = body.invocable_by as InvocableBy;
    if (limits) row.limits = Object.fromEntries(Object.entries(limits).filter(([, v]) => v !== null));
    return null;
  }

  /** A member's role in `organisation`: their row's, else admin for its creator, editor for
   * anyone else. */
  roleOf(userId: number): Role {
    const row = this.organisation.members.find((m) => m.user.id === userId);
    return row?.role ?? (this.organisation.creator?.id === userId ? "admin" : "editor");
  }

  /** Give `me` this role in `organisation`, as an admin changing it would. */
  actAs(role: Role) {
    const row = this.organisation.members.find((m) => m.user.id === this.me.id);
    if (row) row.role = role;
  }

  /** As the API refuses an action a role can't take (`aios.permissions`). */
  private static ADMINS_ONLY = "change the organisation's roles, invitations, name or settings, or delete it";

  private refuseUnlessAdmin(): string | null {
    const role = this.roleOf(this.me.id);
    if (role === "admin") return null;
    return `${role === "editor" ? "Editors" : "Participants"} can't ${FakeApi.ADMINS_ONLY}. Ask an admin.`;
  }

  private refuseUnlessBuilder(): string | null {
    if (this.roleOf(this.me.id) !== "participant") return null;
    return "Participants can't create, edit, archive or unarchive agents, skills, workflows and schedules. Ask an editor or an admin.";
  }

  private approvalRulesOut() {
    return Object.entries(DEFAULT_RULES).map(([kind, default_role]) => ({
      kind,
      label: RULE_LABELS[kind],
      role: this.approvalRules[kind],
      default_role,
      updated_at: null,
    }));
  }

  private inviteOut({ token: _token, ...invite }: InviteRow) {
    return { ...invite, role: invite.role ?? "editor" };
  }

  /** The signed-in user as `/api/me` (and signing in) answers. */
  private meOut(account: Account) {
    const others = this.otherMemberships.map((o) => ({
      ...o,
      role: o.role ?? (o.is_creator ? "admin" : "editor"),
      deletes_at: o.deletes_at ?? null,
    }));
    if (!this.member) return { ...accountOut(account), organisations: others };
    const { id, name, slug, is_creator, role } = this.organisationSummary();
    const own = { id, name, slug, is_creator, role, deletes_at: this.organisation.deletion?.deletes_at ?? null };
    const organisations = this.joinedEarlier ? [own, ...others] : [...others, own];
    return { ...accountOut(account), organisations };
  }

  /**
   * `me` enters `organisation` (made or joined just now): the one they were in, if any, stays
   * theirs, listed after it as joined earlier.
   */
  private enter(organisation: OrganisationRow) {
    if (this.member) {
      const { id, name, slug, is_creator, role } = this.organisationSummary();
      const earlier = { id, name, slug, is_creator, role, deletes_at: this.organisation.deletion?.deletes_at ?? null };
      this.otherMemberships = this.joinedEarlier ? [earlier, ...this.otherMemberships] : [...this.otherMemberships, earlier];
    }
    this.joinedEarlier = true;
    this.organisation = organisation;
    this.member = true;
  }

  /**
   * As registering does: the new account joins the organisation the invitation link `token` is
   * for, if it opens (`invites` or `invitations`), which becomes the fake's `organisation`; else
   * it's in none.
   */
  private joinOnRegistering(account: Account, token: string | undefined) {
    const now = new Date().toISOString();
    const own = token ? this.invites.find((i) => i.token === token) : undefined;
    const other = token ? this.invitations.get(token) : undefined;
    this.otherMemberships = [];
    if (own) {
      this.organisation.members.push({ user: account, joined_at: now, invite_id: own.id, role: own.role ?? "editor" });
      this.member = true;
    } else if (other) {
      this.organisation = {
        ...other.organisation,
        slug: slugify(other.organisation.name),
        creator: null,
        members: [{ user: account, joined_at: now, invite_id: null }],
        created_at: now,
      };
      this.member = true;
    } else {
      this.member = false;
    }
  }

  /** The organisation as `/api/me` carries it. */
  private organisationSummary() {
    const { id, name, slug, creator } = this.organisation;
    return { id, name, slug, is_creator: creator?.id === this.me.id, role: this.roleOf(this.me.id) };
  }

  /**
   * Adds a `data` table. A column given as `name:type` is of that type (`vector(3)` is an
   * embedding), as `name` alone, text.
   */
  addTable(
    name: string,
    columns: (string | Partial<TableColumnRow>)[],
    rows: (string | null)[][] = [],
    fields: Partial<TableRow> = {},
  ): TableRow {
    const table: TableRow = {
      name,
      description: null,
      columns: columns.map((c) => {
        if (typeof c !== "string") {
          const defaults = { name: "", type: "text", nullable: true, description: null };
          return { ...defaults, embedding: false, ...c };
        }
        const [column, type = "text"] = c.split(":");
        return {
          name: column,
          type,
          nullable: true,
          description: null,
          embedding: /^(vector|halfvec|sparsevec)\b/.test(type),
        };
      }),
      rows,
      ...fields,
    };
    this.dataTables.push(table);
    this.dataTables.sort((a, b) => a.name.localeCompare(b.name));
    return table;
  }

  /** An agent as a table's access names it. */
  private tableAgent(id: number) {
    const agent = this.agents.get(id);
    return { id, name: agent?.name ?? `Agent ${id}`, archived: !!agent?.archived_at };
  }

  /** Who may write the table, as `aios.api.data` lists it. */
  tableAccess(table: TableRow) {
    return {
      name: table.name,
      owner: table.owner == null ? null : this.tableAgent(table.owner),
      shares: (table.shares ?? []).map((id) => this.tableAgent(id)),
      pending: this.accessPending.has(table.name),
    };
  }

  /**
   * Whether a change to a table's access is applied as it's made. Off, it's pending (as the
   * server answers until the urgent lane applies it) until `applyAccess`.
   */
  accessApplied = true;
  private accessPending = new Set<string>();

  /** The urgent lane applies every pending change to tables' access. */
  applyAccess() {
    this.accessPending.clear();
  }

  private accessChanged(name: string) {
    if (!this.accessApplied) this.accessPending.add(name);
  }

  /** Every share, unshare and transfer the app made, as [table, what, agent id or null]. */
  accessChanges: [string, string, number | null][] = [];

  /** Setting a table's owner, sharing and unsharing it, as `aios.api.data` answers them. */
  private changeAccess(
    name: string,
    what: string,
    method: string,
    body: Record<string, unknown> | null,
    shareId?: string,
  ): [number, unknown] {
    const role = this.roleOf(this.me.id);
    if (role === "participant") {
      return [403, { detail: "Participants can't share, unshare or transfer data tables. Ask an editor or an admin." }];
    }
    const table = this.dataTables.find((t) => t.name === name);
    if (!table) return [404, { detail: "No such table" }];
    const agentId = shareId !== undefined ? Number(shareId) : (body?.agent_id as number | null);
    const agent = agentId === null ? undefined : this.agents.get(agentId);
    if (agentId !== null && !agent) return [404, { detail: "No such agent" }];
    const shares = table.shares ?? [];
    if (what === "owner" && method === "PUT") {
      if (agent?.archived_at) return [409, { detail: `The agent ${agent.name} is archived, so it can't own a table.` }];
      if (table.owner != null && table.owner !== agentId && role !== "admin" && this.approvalRules.change_ownership === "admin") {
        return [403, { detail: "Changing a table's owner needs an admin, by the organisation's approval rules." }];
      }
      table.owner = agentId;
      table.shares = shares.filter((id) => id !== agentId);
      this.accessChanges.push([name, "owner", agentId]);
      this.accessChanged(name);
      return [200, this.tableAccess(table)];
    }
    if (what === "shares" && method === "POST" && agent) {
      if (agent.archived_at) return [409, { detail: `The agent ${agent.name} is archived, so it can't be given a table.` }];
      if (table.owner == null) return [409, { detail: `\`data.${name}\` has no owner, so any agent may write it: assign one before sharing it.` }];
      if (table.owner === agent.id) return [409, { detail: `The agent ${agent.name} owns \`data.${name}\` already.` }];
      if (shares.includes(agent.id)) return [409, { detail: `\`data.${name}\` is shared with the agent ${agent.name} already.` }];
      table.shares = [...shares, agent.id];
      this.accessChanges.push([name, "share", agent.id]);
      this.accessChanged(name);
      return [201, this.tableAccess(table)];
    }
    if (what === "shares" && method === "DELETE" && agentId !== null) {
      if (!shares.includes(agentId)) return [404, { detail: "The table isn't shared with that agent" }];
      table.shares = shares.filter((id) => id !== agentId);
      this.accessChanges.push([name, "unshare", agentId]);
      this.accessChanged(name);
      return [200, this.tableAccess(table)];
    }
    return [405, { detail: "Method Not Allowed" }];
  }

  /** The tables endpoints, as `aios.api.data` answers them. */
  private tablesResponse(
    path: string,
    params: URLSearchParams,
    method = "GET",
    body: Record<string, unknown> | null = null,
  ): [number, unknown] {
    if (path === "/api/data/tables") {
      return [
        200,
        {
          tables: this.dataTables.map((t) => ({
            description: t.description,
            columns: t.columns,
            row_count: t.rowCount ?? t.rows.length,
            row_count_estimated: t.estimated ?? false,
            ...this.tableAccess(t),
          })),
          truncated: false,
        },
      ];
    }
    const access = path.match(/^\/api\/data\/tables\/([^/]+)\/(owner|shares)(?:\/(\d+))?$/);
    if (access) return this.changeAccess(decodeURIComponent(access[1]), access[2], method, body, access[3]);
    const m = path.match(/^\/api\/data\/tables\/([^/]+)\/(rows|value)$/);
    const table = m && this.dataTables.find((t) => t.name === decodeURIComponent(m[1]));
    if (!m || !table) return [404, { detail: "No such table" }];
    const columnIndex = (name: string | null) => table.columns.findIndex((c) => c.name === name);
    if (m[2] === "value") {
      const column = columnIndex(params.get("column"));
      if (column < 0) return [422, { detail: `The table has no column '${params.get("column")}'` }];
      const row = table.rows[Number(params.get("row"))];
      if (!row) return [404, { detail: "No such row (any more)" }];
      return [200, { value: row[column], truncated: false }];
    }
    this.rowReads.push(params.toString());
    const offset = Number(params.get("offset") ?? 0);
    const limit = Number(params.get("limit") ?? 50);
    const sort = params.get("sort");
    let order = table.rows.map((_, i) => i);
    if (sort !== null) {
      const column = columnIndex(sort);
      if (column < 0) return [422, { detail: `The table has no column '${sort}'` }];
      const sign = params.get("order") === "desc" ? -1 : 1;
      // Nulls last either way, as the server sorts them.
      order = order.sort((a, b) => {
        const x = table.rows[a][column];
        const y = table.rows[b][column];
        if (x === null || y === null) return x === y ? a - b : x === null ? 1 : -1;
        return sign * x.localeCompare(y, undefined, { numeric: true }) || a - b;
      });
    }
    const page = order.slice(offset, offset + limit);
    return [
      200,
      {
        columns: table.columns.map((c) => c.name),
        rows: page.map((i) => ({
          key: String(i),
          values: table.rows[i].map((v) => (v === null ? null : v.slice(0, PREVIEW_CHARS))),
          truncated: table.rows[i].flatMap((v, j) =>
            v !== null && v.length > PREVIEW_CHARS ? [j] : [],
          ),
        })),
        offset,
        has_more: offset + limit < order.length,
      },
    ];
  }

  /** Adds a document, as if uploaded earlier. */
  /** Adds a source other than Uploads, as later kinds of source will. */
  addSource(name: string, kind: SourceRow["kind"] = "url", fields: Partial<SourceRow> = {}): SourceRow {
    const source: SourceRow = {
      id: this.id(),
      kind,
      name,
      status: "active",
      provider: kind === "connector" ? "test" : null,
      interval_seconds: 900,
      created_at: NOW,
      ...fields,
    };
    this.sources.push(source);
    return source;
  }

  /** Adds a connector, as connecting would. */
  addConnector(provider = "test", fields: Partial<ConnectorRow> = {}): ConnectorRow {
    const title = this.connectorProviders.find((p) => p.provider === provider)?.title ?? provider;
    const connector: ConnectorRow = {
      id: (this.connectors.at(-1)?.id ?? 0) + 1,
      provider,
      name: title,
      status: "active",
      unavailable: null,
      scope: [],
      source_id: null,
      created_at: NOW,
      ...fields,
    };
    this.connectors.push(connector);
    return connector;
  }

  private connectorOut(connector: ConnectorRow) {
    const source = this.sources.find((s) => s.id === connector.source_id);
    return { ...connector, source: source ? this.sourceOut(source) : null };
  }

  /** A sync, as a source's page lists it. */
  sync(fields: Partial<SyncRow> = {}): SyncRow {
    return {
      id: this.id(),
      kind: "incremental",
      status: "succeeded",
      started_at: NOW,
      finished_at: NOW,
      added: 0,
      updated: 0,
      removed: 0,
      skipped: 0,
      dropped: 0,
      awaiting_check: 0,
      storage_full: false,
      error: null,
      ...fields,
    };
  }

  private sourceOut(source: SourceRow) {
    const document_count = this.documents.filter((d) => d.source.id === source.id).length;
    const { syncs = [], webhook_url: _url, ...rest } = source;
    // Uploads, webhook and email sources don't sync.
    const unsynced = ["upload", "webhook", "email"].includes(source.kind);
    return {
      health: "healthy",
      provider: null,
      interval_seconds: unsynced ? null : 900,
      item_count: 0,
      next_sync_at: null,
      ...rest,
      document_count,
      last_sync: syncs[0] ?? null,
    };
  }

  private sourceDetail(source: SourceRow) {
    return { ...this.sourceOut(source), syncs: source.syncs ?? [], webhook_url: source.webhook_url ?? null };
  }

  /** A new webhook secret (with its URL) or email address for the source, answered once. */
  private rotate(source: SourceRow) {
    this.secrets += 1;
    if (source.kind === "webhook") {
      source.webhook_url = `http://localhost:5173/api/webhooks/token${this.secrets}`;
      return { ...this.sourceDetail(source), webhook_secret: `whsec_secret${this.secrets}` };
    }
    return { ...this.sourceDetail(source), email_address: `in-address${this.secrets}@in.aios.test` };
  }

  addDocument(filename: string, fields: Partial<DocumentRow> = {}): DocumentRow {
    const { id, kind, name } = this.sources[0];
    const doc: DocumentRow = {
      id: this.id(),
      filename,
      status: "ready",
      error: null,
      verdict: "clean",
      verdict_reason: null,
      chunk_count: 1,
      size_bytes: 2048,
      uploaded_by: null,
      created_at: NOW,
      ...fields,
      source: fields.source ? { id: fields.source.id, kind: fields.source.kind, name: fields.source.name } : { id, kind, name },
    };
    this.documents.unshift(doc);
    return doc;
  }

  /** Records the documents' bytes, as the API does after an upload or delete. */
  private recordFiles() {
    const files_bytes = this.documents.reduce((sum, d) => sum + d.size_bytes, 0);
    this.dataStoreUsage = { ...this.dataStoreUsage, files_bytes };
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

  /**
   * What the next import (`POST /api/imports`) of any file makes: an agent per item of kind
   * agent (by its imported name, unrunnable if it says so), the others listed only. Unset, an
   * import is a 422, as for a file that isn't an export.
   */
  nextImport: { kind: "skill" | "agent" | "workflow"; name: string; imported_name?: string; unrunnable?: string }[] | null =
    null;
  /** The files imported (`dry_run` false) or previewed, by name, in order. */
  imports: { filename: string; dryRun: boolean }[] = [];

  addAgent(name: string, versions = 1, description = ""): AgentRow {
    const agent: AgentRow = {
      id: this.id(),
      name,
      description,
      base_prompt: `You are ${name}.`,
      versions: [],
      tools: [],
      created_by: this.me,
      updated_by: this.me,
      archived_at: null,
      archived_by: null,
    };
    for (let n = 1; n <= versions; n++) {
      const vid = this.id();
      agent.versions.push(vid);
      this.agentVersions.set(vid, [agent.id, n]);
    }
    this.agents.set(agent.id, agent);
    return agent;
  }

  /**
   * Adds a skill with `versions` versions, each with its own instructions ("<name> instructions, vN.")
   * and `description` (by default "Use when writing <name>.").
   */
  addSkill(name: string, versions = 1, description = `Use when writing ${name.toLowerCase()}.`): SkillRow {
    const skill: SkillRow = {
      id: this.id(),
      name,
      versions: [],
      usedBy: [],
      archived_at: null,
      archived_by: null,
    };
    for (let n = 1; n <= versions; n++) {
      skill.versions.push({
        id: this.id(),
        version: n,
        note: null,
        name,
        description,
        content: `${name} instructions, v${n}.`,
      });
    }
    this.skills.set(skill.id, skill);
    return skill;
  }

  /** Adds a tool to the library: with no settings, unless given their schema and defaults. */
  addTool(key: string, name: string, config_schema: object = {}, default_config: object = {}) {
    this.tools.push({ key, name, description: `${name}.`, config_schema, default_config });
  }

  /** Gives `agent`'s current version the tool `key`. */
  addToolUse(agent: AgentRow, key: string) {
    agent.tools.push(key);
  }

  /** Has `agent`'s current version use `skill` at `version` (by default its latest). */
  addSkillUse(skill: SkillRow, agent: AgentRow, version = skill.versions.length) {
    skill.usedBy.push({ agent, version });
  }

  private skillOut(skill: SkillRow) {
    const current = skill.versions.at(-1)!;
    return {
      id: skill.id,
      name: skill.name,
      created_at: NOW,
      updated_at: NOW,
      current_version: { ...current, created_at: NOW },
      archived_at: skill.archived_at,
      archived_by: skill.archived_by,
      agents: this.skillUsage(skill),
    };
  }

  /** The agents whose current version uses the skill, as the API lists them. */
  private skillUsage(skill: SkillRow) {
    return skill.usedBy.map(({ agent, version }) => ({
      agent_id: agent.id,
      agent_name: agent.name,
      agent_version: agent.versions.length,
      skill_version: version,
    }));
  }

  /** Adds an enabled document ingested trigger of the agent or workflow, with `fields` set. */
  addEventTrigger(
    kind: "agent" | "workflow",
    target: AgentRow | WorkflowRow,
    fields: Record<string, unknown> = {},
  ) {
    const trigger = {
      ...this.triggerOut({
        kind: "event",
        target_kind: kind,
        target_id: target.id,
        enabled: true,
        input: "",
        cron: null,
        timezone: "UTC",
        event_type: "document_ingested",
        filter: {},
        fire_on_own_events: false,
      }),
      ...fields,
    };
    this.schedules.push(trigger);
    return trigger;
  }

  /** Webhook triggers' rotations so far, by trigger id. */
  webhookRotations = new Map<number, number>();

  /** A chat message trigger's agent or workflow, added to the chat it listens to (as the API
   * does), whether or not `me` is in it. */
  private addListener(trigger: Record<string, unknown>) {
    const chatId = (trigger.filter as { chat_id?: number }).chat_id;
    const chat = chatId ? this.chats.get(chatId) : undefined;
    if (!chat || trigger.event_type !== "chat_message") return;
    const kind = trigger.target_kind as Kind;
    const id = trigger.target_id as number;
    if (!chat.members.some((m) => m.kind === kind && "target_id" in m && m.target_id === id))
      this.addChatTarget(chat, kind, { id });
  }

  /** A trigger as the API returns it, from what a form sent (`TriggerInput`). */
  private triggerOut(body: Record<string, unknown>, id = this.id()): Record<string, unknown> {
    const target = this.chatTarget(body.target_kind as Kind, Number(body.target_id));
    const isEvent = body.kind === "event";
    return {
      id,
      kind: body.kind,
      target_kind: body.target_kind,
      target_id: body.target_id,
      target_name: target?.name ?? "",
      enabled: body.enabled,
      input: body.input ?? "",
      cron: isEvent ? null : body.cron,
      description: isEvent ? EVENT_DESCRIPTIONS[body.event_type as string] : `At "${body.cron}"`,
      timezone: isEvent ? null : body.timezone,
      event_type: isEvent ? body.event_type : null,
      filter: isEvent ? normalisedFilter(body.filter as Record<string, unknown>) : {},
      fire_on_own_events: isEvent ? !!body.fire_on_own_events : false,
      debounce_seconds: isEvent ? ((body.debounce_seconds as number | null | undefined) ?? null) : null,
      // Listening to webhook sources, a webhook trigger has no URL of its own.
      webhook_url:
        isEvent &&
        body.event_type === "webhook_received" &&
        !((body.filter as { sources?: number[] } | undefined)?.sources ?? []).length
          ? `${WEBHOOK_URL}token-${id}`
          : null,
      disabled_reason: null,
      next_run_at: isEvent || !body.enabled ? null : NOW,
      last_fired_at: null,
      last_status: null,
      last_error: null,
      last_run_id: null,
      last_workflow_run_id: null,
      ...this.destination(body.deliver_to as number | null | undefined),
      created_at: NOW,
      updated_at: NOW,
    };
  }

  /** The chat a trigger delivers to, as the API returns it, and who chose it (`me`). */
  private destination(chatId: number | null | undefined) {
    const chat = chatId ? this.chats.get(chatId) : undefined;
    return {
      deliver_to: chat
        ? { id: chat.id, name: chat.name, archived: !!chat.archived_at }
        : null,
      deliver_to_set_by: chat
        ? { id: this.me.id, name: fullName(this.me) }
        : null,
      delivery_problem: null as string | null,
    };
  }

  /** Adds an enabled daily schedule of the agent or workflow. */
  addSchedule(kind: "agent" | "workflow", target: AgentRow | WorkflowRow) {
    this.schedules.push({
      id: this.id(),
      kind: "schedule",
      target_kind: kind,
      target_id: target.id,
      target_name: target.name,
      enabled: true,
      input: "Go",
      cron: "0 9 * * *",
      description: "At 09:00",
      timezone: "UTC",
      next_run_at: NOW,
      last_fired_at: null,
      last_status: null,
      last_error: null,
      last_run_id: null,
      last_workflow_run_id: null,
      created_at: NOW,
      updated_at: NOW,
    });
  }

  /** Why a chat can't be chosen to deliver to (it's archived), as the API refuses it; else null. */
  private archivedDestination(chatId: unknown): string | null {
    const chat =
      typeof chatId === "number" ? this.chats.get(chatId) : undefined;
    return chat?.archived_at
      ? `${chat.name} is archived, so results can't be delivered there. Choose another chat.`
      : null;
  }

  /** The documents assistant `me` has, made the first time a chat needs it (or again once deleted). */
  documentsAssistant(): AgentRow {
    const live = this.assistantId === null ? undefined : this.agents.get(this.assistantId);
    if (live) return live;
    const agent = this.addAgent(
      "Documents assistant",
      1,
      "Answers from the organisation's documents. Built in: it joins each new chat.",
    );
    this.assistantId = agent.id;
    return agent;
  }

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
      needs_role: "editor",
      impact: [],
      ...fields,
    };
    this.schemaChanges.push(request);
    if (request.status === "pending" && request.decider) {
      this.addNotification({ ref: { approval_id: request.id } });
    }
    return request;
  }

  /**
   * Adds a workflow approval step's approval, pending unless `fields` say otherwise: the
   * approval node `path` of `run` waits on it (that step is added, waiting, unless it's there).
   */
  addApprovalStep(
    run: RunRow,
    path: string[],
    fields: Partial<SchemaChangeRow> = {},
  ): SchemaChangeRow {
    const workflow = this.workflows.get(run.workflow_id)!;
    let step = run.steps.find((s) => s.path.join("/") === path.join("/"));
    const id = `apr-${this.id()}`;
    if (!step) {
      step = this.addStep(run, { path, kind: "approval", status: "waiting" });
    }
    Object.assign(step, { waiting_on_request_ids: [id], approval_id: id });
    return this.addSchemaChange({
      id,
      kind: "workflow_step",
      summary: "Spend 250 on laptops?",
      reason: "Spend 250 on laptops?\nThe risk is high.",
      sql: null as unknown as string,
      ops: null as unknown as Record<string, unknown>[],
      input: { risk: "high", amount: 250 },
      step: {
        workflow_id: workflow.id,
        workflow_name: workflow.name,
        run_id: run.id,
        step_id: step.id,
        path,
      },
      needs_role: "admin",
      expires_at: "2026-10-21T09:00:00Z",
      ...fields,
    });
  }

  /** Marks the approval's notifications read, for everyone: it no longer waits. */
  private clearApprovalNotifications(id: string) {
    for (const n of this.notifications) {
      if (n.kind === "approval_needed" && n.ref.approval_id === id && !n.read_at) n.read_at = NOW;
    }
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
    const { purge_in_days, refuseRestore: _, stallRestore: __, ...fields } = item;
    return { ...fields, purge_at: new Date(Date.now() + purge_in_days * 86400_000).toISOString() };
  }

  /** Adds a notification: the user's own, an approval needed, unless told otherwise. */
  addNotification(fields: Partial<NotificationRow> = {}): NotificationRow {
    const notification: NotificationRow = {
      id: `note-${this.id()}`,
      organisation: { id: this.organisation.id, slug: this.organisation.slug, name: this.organisation.name },
      kind: "approval_needed",
      ref: {},
      title: null,
      detail: null,
      // Later ones are newer, so they list first.
      created_at: new Date(Date.parse(NOW) + this.notifications.length * 1000).toISOString(),
      read_at: null,
      mine: true,
      ...fields,
    };
    this.notifications.push(notification);
    return notification;
  }

  /** As the API lists it, what it says read now; null if what it's about has gone (an approval
   * of the fake's organisation whose request doesn't exist). */
  private notificationOut({ mine: _mine, ...n }: NotificationRow) {
    if (n.kind !== "approval_needed" || n.organisation.id !== this.organisation.id) return n;
    const request = this.schemaChanges.find((r) => r.id === n.ref.approval_id);
    if (!request) return null;
    const title =
      request.kind === "workflow_step" ? "An approval is waiting for you" : "A schema change is waiting for you";
    return { ...n, title, detail: request.summary };
  }

  /** The user's own, as the API lists them: unread first, newest first; gone ones dropped and read. */
  private notificationList() {
    const mine = this.notifications.filter((n) => n.mine).reverse();
    const ordered = [...mine.filter((n) => !n.read_at), ...mine.filter((n) => n.read_at)];
    const listed = [];
    for (const n of ordered) {
      const out = this.notificationOut(n);
      if (out) listed.push(out);
      else n.read_at ??= NOW;
    }
    return { unread: mine.filter((n) => !n.read_at).length, notifications: listed };
  }

  /** What the person's stream sends of the user's notifications: one `notification` event if they
   * changed since it last opened (the first opening only notes them). */
  private notificationChanges(): [string, unknown, number | null][] {
    const mine = this.notifications.filter((n) => n.mine);
    const now = new Map(mine.map((n) => [n.id, n.read_at !== null]));
    const before = this.notificationsSeen;
    this.notificationsSeen = now;
    if (before === null) return [];
    const added = [...now].some(([id, read]) => !read && before.get(id) !== false);
    const read = [...now].some(([id, isRead]) => isRead && before.get(id) === false);
    if (!added && !read) return [];
    const unread = mine.filter((n) => !n.read_at).length;
    return [["notification", { change: added ? "new" : "read", unread }, null]];
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
      kind: "schema_change",
      step: null,
      input: null,
      approver_role: request.needs_role,
      ...fields,
      run: run ? { agent_id: run.agent_id, run_id: run.id, title: run.title } : null,
      can_decide: decider && request.status === "pending" && this.decides(request.needs_role),
    };
  }

  /** Whether `me` may decide a request needing `needs` (an editor's or an admin's). */
  private decides(needs: Role): boolean {
    const role = this.roleOf(this.me.id);
    return role === "admin" || (role === "editor" && needs === "editor");
  }

  /** Adds an entry to the audit log, newest first; by `me` unless `fields` say otherwise. */
  addAuditEntry(fields: Partial<AuditEntryRow> & Pick<AuditEntryRow, "action">): AuditEntryRow {
    const entry: AuditEntryRow = {
      id: this.id(),
      at: NOW,
      actor: { kind: "person", user: this.me, agent: null, run_id: null },
      target_kind: null,
      target_id: null,
      details: {},
      ...fields,
    };
    this.auditEntries = [entry, ...this.auditEntries].sort((a, b) => b.at.localeCompare(a.at));
    return entry;
  }

  /**
   * Adds a chat run of the agent, at its current version, started by `me` unless `fields` say
   * otherwise; not tracked unless they say.
   */
  addAgentRun(
    agent: AgentRow,
    title: string | null = null,
    message_count = 0,
    fields: Partial<AgentRunCounters & Pick<AgentRunRow, "created_by" | "trigger_id" | "chat_id">> = {},
  ): AgentRunRow {
    const run: AgentRunRow = {
      id: this.id(),
      agent_id: agent.id,
      agent_version_id: agent.versions.at(-1)!,
      title,
      message_count,
      created_by: this.me,
      trigger_id: null,
      ui_messages: [],
      turns: [],
      ...NOT_TRACKED,
      ...fields,
    };
    this.agentRuns.set(run.id, run);
    return run;
  }

  /** Adds something the agent remembered, learned in a run with no chat unless `fields` say. */
  addMemory(agent: AgentRow, content: string, fields: Partial<MemoryRow> = {}): MemoryRow {
    const memory: MemoryRow = {
      id: this.id(),
      agent_id: agent.id,
      content,
      agent_run_id: null,
      run_title: null,
      turn_id: null,
      chat_id: null,
      chat_name: null,
      created_at: NOW,
      updated_at: NOW,
      updated_by: null,
      ...fields,
    };
    this.memories.set(memory.id, memory);
    return memory;
  }

  /** Adds a turn to the run, on its version; it doesn't change the run's own counters. */
  addTurn(run: AgentRunRow, fields: Partial<TurnRow> = {}): TurnRow {
    const turn: TurnRow = {
      id: this.id(),
      invocation_id: null,
      agent_version_id: run.agent_version_id,
      status: "succeeded",
      error: null,
      started_at: NOW,
      finished_at: NOW,
      model_calls: 0,
      tool_calls: 0,
      input_tokens: 0,
      output_tokens: 0,
      ...fields,
    };
    run.turns.push(turn);
    return turn;
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
      archived_at: null,
      archived_by: null,
      created_at: NOW,
      updated_at: NOW,
      created_by: this.me,
      updated_by: this.me,
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
    this.saveVersion(workflow, next.nodes, next.edges, note, next);
  }

  /** A newer version of the agent, as editing it would make. */
  bumpAgent(agent: AgentRow) {
    const vid = this.id();
    agent.versions.push(vid);
    this.agentVersions.set(vid, [agent.id, agent.versions.length]);
  }

  /** A run of the workflow's current version, started by `me` unless `run` says otherwise;
   * steps take their kind from the graph. */
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
      waiting_since: null,
      waited_seconds: 0,
      created_by: this.me,
      trigger_id: null,
      stale_pins: [],
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
      waiting_on_request_ids: [],
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

  /**
   * Adds a chat of `organisation`, made by `me` unless said otherwise (`owner`: who made it), who
   * is its first member.
   */
  addChat(
    name: string,
    {
      description = "",
      owner = this.me,
      organisation_id = this.organisation.id,
    }: { description?: string; owner?: ChatUser; organisation_id?: number } = {},
  ): ChatRow {
    const chat: ChatRow = {
      id: this.id(),
      organisation_id,
      created_by: owner,
      updated_by: owner,
      name,
      description,
      members: [],
      messages: [],
      created_at: NOW,
      updated_at: NOW,
    };
    chat.members.push({ id: this.id(), kind: "user", user: owner, added_at: NOW, last_read: null });
    this.chats.set(chat.id, chat);
    return chat;
  }

  /** Posts a message to a chat, as `author` (by default `me`) would, mentioning `mentions`. */
  addChatMessage(
    chat: ChatRow,
    body: string,
    author: ChatAuthor = this.me,
    mentions: ChatMention[] = [],
    extra: Partial<Pick<ChatMessageRow, "kind" | "invocation_id">> = {},
  ): ChatMessageRow {
    const message: ChatMessageRow = {
      id: this.id(),
      author,
      kind: "text",
      body,
      mentions,
      invocation_id: null,
      created_at: NOW,
      ...extra,
    };
    chat.messages.push(message);
    return message;
  }

  /** An invocation's run card: its acknowledgement. */
  private cardOf(chat: ChatRow, invocation: InvocationRow): ChatMessageRow {
    return chat.messages.find((m) => m.kind === "run_card" && m.invocation_id === invocation.id)!;
  }

  /** The agent or workflow an invocation is of. */
  private targetOf(invocation: InvocationRow): AgentRow | WorkflowRow {
    return (invocation.target_kind === "agent" ? this.agents : this.workflows).get(invocation.target_id)!;
  }

  /** Queues an invocation of the agent or workflow for a message mentioning it, with its card. */
  private invoke(
    chat: ChatRow,
    message: ChatMessageRow,
    kind: Kind,
    target: AgentRow | WorkflowRow,
    refusal?: string,
  ): InvocationRow {
    const invocation: InvocationRow = {
      id: this.id(),
      chat_id: chat.id,
      message_id: message.id,
      requested_by: this.me.id,
      target_kind: kind,
      target_id: target.id,
      // A mention its guards refused: nothing runs, and its card says why.
      status: refusal ? "refused" : "queued",
      error: null,
      refusal_reason: refusal ?? null,
      brief: null,
      run_id: null,
      created_at: NOW,
      started_at: null,
      finished_at: refusal ? NOW : null,
    };
    this.invocations.set(invocation.id, invocation);
    this.addChatMessage(
      chat,
      refusal ?? ACKNOWLEDGEMENT,
      { kind, id: target.id, name: target.name },
      [],
      {
        kind: "run_card",
        invocation_id: invocation.id,
      },
    );
    // An archived agent's or workflow's is refused at once, as the API's is: no handoff, no run.
    if (!refusal && target.archived_at !== null) {
      const reason = `The ${kind} ${target.name} is archived.`;
      Object.assign(invocation, { status: "refused", refusal_reason: reason, finished_at: NOW });
      this.cardOf(chat, invocation).body = reason;
    }
    return invocation;
  }

  /**
   * Ends an invocation and posts its result back from its agent or workflow, as the API does: a
   * workflow's result, and an agent's reply in JSON or over the threshold, is handed back first
   * (`handing_back`), and posted summarised on the next beat (`handBack`).
   */
  private finishInvocation(invocation: InvocationRow, status: "succeeded" | "failed", body: string, error: string | null = null) {
    // What the handback answered is posted as it is.
    if (status === "succeeded" && invocation.status !== "handing_back" && handsBack(invocation, body)) {
      Object.assign(invocation, { status: "handing_back", full_output: body });
      this.drafts.delete(invocation.id);
      return;
    }
    Object.assign(invocation, { status, error, finished_at: NOW });
    // The posted reply (or the failure) replaces the draft.
    this.drafts.delete(invocation.id);
    const target = this.targetOf(invocation);
    this.addChatMessage(
      this.chats.get(invocation.chat_id)!,
      body,
      { kind: invocation.target_kind, id: target.id, name: target.name },
      [],
      { invocation_id: invocation.id },
    );
  }

  /** Posts what the handback of an invocation handing back answers, as `handbacks` scripts it. */
  private handBack(invocation: InvocationRow) {
    const output = invocation.full_output ?? "";
    const handback = this.handbacks.get(invocation.target_id) ?? {
      summary: output,
    };
    const body =
      "error" in handback
        ? `${output}\n\n_Couldn't summarise this result (${handback.error}), so it's posted as it came._`
        : handback.summary;
    Object.assign(invocation, {
      summary: "error" in handback ? null : handback.summary,
      handback_error: "error" in handback ? handback.error : null,
    });
    this.finishInvocation(invocation, "succeeded", body);
  }

  /**
   * A schedule's run whose result was delivered to the chat: its summary posted from the agent or
   * workflow, with its run's card (collapsed) under it and the full output behind it.
   */
  addDelivery(
    chat: ChatRow,
    kind: Kind,
    target: AgentRow | WorkflowRow,
    summary: string,
    fullOutput: string,
  ) {
    const run =
      kind === "agent"
        ? this.addAgentRun(target as AgentRow, "Go", 2, {
            status: "succeeded",
            started_at: NOW,
            finished_at: NOW,
          })
        : this.addRun(target as WorkflowRow, {
            status: "succeeded",
            output: fullOutput,
          });
    const invocation: InvocationRow = {
      id: this.id(),
      chat_id: chat.id,
      message_id: null,
      delivered: true,
      requested_by: this.me.id,
      target_kind: kind,
      target_id: target.id,
      status: "succeeded",
      error: null,
      brief: null,
      full_output: fullOutput,
      summary,
      run_id: run.id,
      created_at: NOW,
      started_at: NOW,
      finished_at: NOW,
    };
    this.invocations.set(invocation.id, invocation);
    this.addChatMessage(
      chat,
      summary,
      { kind, id: target.id, name: target.name },
      [],
      {
        invocation_id: invocation.id,
      },
    );
    return invocation;
  }

  /**
   * The handoff of a briefing invocation: its brief kept and its ack in place of the card's
   * placeholder, as `handoffs` scripts it. False if it failed, which fails the invocation.
   */
  private handOff(chat: ChatRow, invocation: InvocationRow): boolean {
    const handoff = this.handoffs.get(invocation.target_id) ?? HANDOFF;
    if ("error" in handoff) {
      const error = `The handoff failed: ${handoff.error}`;
      this.finishInvocation(invocation, "failed", `Couldn't start the run: ${error}`, error);
      return false;
    }
    invocation.brief = handoff.brief || null;
    this.cardOf(chat, invocation).body = handoff.ack;
    return true;
  }

  /**
   * Moves the chat's unfinished invocations on by one beat, as each read of its messages does: a
   * queued one goes briefing; a briefing one is handed off (`handOff`) and starts its run (or
   * fails, if `refuseStart` says so); a running one's run moves on (`advance`), and once it has
   * ended, the result is posted back. One whose run was deleted fails.
   */
  advanceInvocations(chat: ChatRow) {
    for (const invocation of this.invocations.values()) {
      if (invocation.chat_id !== chat.id || invocation.status === "refused" || invocation.delivered) continue;
      if (invocation.status === "handing_back") {
        this.handBack(invocation);
        continue;
      }
      // A mention of an agent waits its turn behind the earlier ones in the chat.
      if (invocation.status === "queued" && this.waitingFor(invocation)) continue;
      if (invocation.status === "queued") {
        invocation.status = "briefing";
        continue;
      }
      if (invocation.status === "briefing" && !this.handOff(chat, invocation)) continue;
      if (invocation.target_kind === "agent") {
        this.advanceTurn(chat, invocation);
        continue;
      }
      if (invocation.status === "briefing") {
        const refused = this.refuseStart.get(invocation.target_id);
        if (refused !== undefined) {
          this.finishInvocation(invocation, "failed", `Couldn't start the run: ${refused}`, refused);
          continue;
        }
        const message = chat.messages.find((m) => m.id === invocation.message_id)!;
        const run = this.addRun(this.workflows.get(invocation.target_id)!, {
          status: "queued",
          input: runInput(invocation.brief, message.body),
          started_at: null,
        });
        Object.assign(invocation, { status: "running", run_id: run.id, started_at: NOW });
      } else if (invocation.status === "running") {
        const run = this.runs.get(invocation.run_id!);
        if (!run) {
          const deleted = "The run was deleted before it finished.";
          this.finishInvocation(invocation, "failed", `The run failed: ${deleted}`, deleted);
          continue;
        }
        this.advance(run);
        if (run.status === "succeeded") this.finishInvocation(invocation, "succeeded", run.output ?? "");
        else if (run.status === "failed") {
          this.finishInvocation(invocation, "failed", `The run failed: ${run.error}`, run.error);
        }
      }
    }
  }

  /**
   * Moves a mentioned agent's turn on by one read: a briefed one starts a run, running from now (so
   * its card's elapsed time ticks from the real clock); each read after is a model step adding a
   * tool call and tokens, until its `agentTurns` steps are done and its reply (or error) is
   * posted back. One whose run was deleted fails.
   */
  private advanceTurn(chat: ChatRow, invocation: InvocationRow) {
    const agent = this.agents.get(invocation.target_id)!;
    const turn = this.agentTurns.get(agent.id) ?? AGENT_TURN;
    if (invocation.status === "briefing") {
      const message = chat.messages.find((m) => m.id === invocation.message_id)!;
      const input = runInput(invocation.brief, message.body);
      const started_at = new Date().toISOString();
      const counters = { status: "running" as const, started_at, finished_at: null, tool_call_count: 0, input_tokens: 0, output_tokens: 0 };
      // The agent's conversation run in the chat: a later mention is another turn of it, on the
      // agent's current version.
      let run = this.conversationRun(agent, chat);
      if (run) {
        Object.assign(run, { ...counters, agent_version_id: agent.versions.at(-1)! });
      } else {
        // Titled after what was first asked, not the brief.
        run = this.addAgentRun(agent, messageText(message.body), 0, { ...counters, chat_id: chat.id });
      }
      const n = run.ui_messages.length;
      run.ui_messages = [...run.ui_messages, { id: `u${n}`, role: "user", parts: [{ type: "text", text: input }] }];
      run.message_count = run.ui_messages.length;
      this.addTurn(run, { invocation_id: invocation.id, status: "running", started_at, finished_at: null });
      Object.assign(invocation, { status: "running", run_id: run.id, started_at });
    } else if (invocation.status === "running") {
      const run = this.agentRuns.get(invocation.run_id!);
      if (!run) {
        const deleted = "The run was deleted before it finished.";
        this.finishInvocation(invocation, "failed", `The run failed: ${deleted}`, deleted);
        return;
      }
      // A turn that ended waiting on an approval carries on once it's decided.
      if (this.runWaiting(run)) return;
      const current = this.turnOf(run, invocation);
      const done = current?.tool_calls ?? run.tool_call_count ?? 0;
      if (done < turn.steps) {
        if (turn.drafts?.length) {
          this.drafts.set(invocation.id, turn.drafts[Math.min(done, turn.drafts.length - 1)]);
        }
        run.tool_call_count = (run.tool_call_count ?? 0) + 1;
        run.input_tokens = (run.input_tokens ?? 0) + turn.tokensPerStep - 20;
        run.output_tokens = (run.output_tokens ?? 0) + 20;
        if (current) {
          current.model_calls += 1;
          current.tool_calls += 1;
          current.input_tokens += turn.tokensPerStep - 20;
          current.output_tokens += 20;
        }
        return;
      }
      const finished_at = new Date().toISOString();
      if (turn.error) {
        Object.assign(run, { status: "failed", finished_at });
        if (current) Object.assign(current, { status: "failed", error: turn.error, finished_at });
        this.finishInvocation(invocation, "failed", `The run failed: ${turn.error}`, turn.error);
        return;
      }
      const reply = turn.reply ?? `Done: ${messageText(chat.messages.find((m) => m.id === invocation.message_id)?.body ?? "")}`;
      const n = run.ui_messages.length;
      run.ui_messages = [...run.ui_messages, { id: `a${n}`, role: "assistant", parts: [{ type: "text", text: reply }] }];
      Object.assign(run, { status: "succeeded", finished_at, message_count: run.ui_messages.length });
      if (current) Object.assign(current, { status: "succeeded", finished_at });
      this.finishInvocation(invocation, "succeeded", reply);
    }
  }

  /** The agent's conversation run in the chat, if it has one. */
  private conversationRun(agent: AgentRow, chat: ChatRow): AgentRunRow | undefined {
    return [...this.agentRuns.values()].find((r) => r.agent_id === agent.id && r.chat_id === chat.id);
  }

  /** The run's latest turn for the invocation, if it has begun one. */
  private turnOf(run: AgentRunRow, invocation: InvocationRow): TurnRow | undefined {
    return run.turns.filter((t) => t.invocation_id === invocation.id).at(-1);
  }

  /** Whether the agent run waits on an approval (a schema change it asked for). */
  private runWaiting(run: AgentRunRow): boolean {
    return this.schemaChanges.some((r) => r.run_id === run.id && !r.resumed_at && r.status !== "cancelled");
  }

  /**
   * What a mention of an agent not started yet waits for, as the API says: `approval` while the
   * agent's run in the chat waits on one, `turn` while an earlier mention of it there hasn't
   * finished; null if it waits for nothing.
   */
  private waitingFor(invocation: InvocationRow): "approval" | "turn" | null {
    if (invocation.target_kind !== "agent" || invocation.delivered) return null;
    if (invocation.status !== "queued" && invocation.status !== "briefing") return null;
    const earlier = [...this.invocations.values()].some(
      (i) =>
        i.id < invocation.id &&
        i.chat_id === invocation.chat_id &&
        i.target_kind === "agent" &&
        i.target_id === invocation.target_id &&
        !i.delivered &&
        ["queued", "briefing", "running", "handing_back"].includes(i.status),
    );
    if (!earlier) return null;
    const agent = this.agents.get(invocation.target_id)!;
    const run = this.conversationRun(agent, this.chats.get(invocation.chat_id)!);
    return run && this.runWaiting(run) ? "approval" : "turn";
  }

  /** Adds a user to a chat, as joining (or a member adding them) would. */
  addChatMember(chat: ChatRow, user: ChatUser): ChatUserMemberRow {
    const member: ChatUserMemberRow = { id: this.id(), kind: "user", user, added_at: NOW, last_read: null };
    chat.members.push(member);
    return member;
  }

  /**
   * A run card in the chat for a run already made: a message from `me` mentioning the agent or
   * workflow, and its invocation, finished, pointing at the run.
   */
  addChatRunCard(chat: ChatRow, kind: Kind, target: AgentRow | WorkflowRow, runId: number): InvocationRow {
    const message = this.addChatMessage(chat, `@[${target.name}](${kind}:${target.id}) Go`, this.me, [
      { kind, id: target.id, name: target.name },
    ]);
    const invocation = this.invoke(chat, message, kind, target);
    this.cardOf(chat, invocation).body = "On it.";
    Object.assign(invocation, { status: "succeeded", run_id: runId, started_at: NOW, finished_at: NOW });
    return invocation;
  }

  /** Adds an agent or a workflow to a chat, as a member would. */
  addChatTarget(chat: ChatRow, kind: Kind, target: { id: number }): ChatMemberRow {
    const member: ChatMemberRow = { id: this.id(), kind, target_id: target.id, added_at: NOW };
    chat.members.push(member);
    return member;
  }

  private saveVersion(
    workflow: WorkflowRow,
    nodes: NodeIn[],
    edges: Edge[],
    note: string | null,
    schemas: { input_schema?: Json | null; output_schema?: Json | null } = {},
  ) {
    const hash = (n: NodeIn[], e: Edge[], s: typeof schemas) =>
      JSON.stringify([
        n
          .map(
            (x) =>
              `${x.key}/${x.kind}/${x.version_id}/${JSON.stringify([x.input_schema ?? null, x.output_schema ?? null, x.input_template ?? null, x.prompt_template ?? null, x.approver_role ?? null, x.expiry_days ?? null])}`,
          )
          .sort(),
        e.map((x) => `${x.source}>${x.target}/${JSON.stringify(x.condition ?? null)}`).sort(),
        JSON.stringify([s.input_schema ?? null, s.output_schema ?? null]),
      ]);
    if (workflow.versions.length) {
      const cur = this.current(workflow);
      if (hash(cur.nodes, cur.edges, cur) === hash(nodes, edges, schemas)) return;
    }
    const version: WorkflowVersionRow = {
      id: this.id(),
      workflow_id: workflow.id,
      version: workflow.versions.length + 1,
      note,
      created_at: NOW,
      nodes,
      edges,
      input_schema: schemas.input_schema ?? null,
      output_schema: schemas.output_schema ?? null,
    };
    this.workflowVersions.set(version.id, version);
    workflow.versions.push(version.id);
  }

  // --- Responses, shaped like the server's -------------------------------------------------

  private agentRunsOf(agent: AgentRow) {
    return [...this.agentRuns.values()].filter((r) => r.agent_id === agent.id);
  }

  private agentRunOut(run: AgentRunRow) {
    const { ui_messages: _, turns: __, ...summary } = run;
    const invocation = [...this.invocations.values()].find(
      (i) => i.target_kind === "agent" && i.run_id === run.id,
    );
    const chat = invocation && this.chats.get(invocation.chat_id);
    return {
      ...summary,
      chat_id: chat?.id ?? null,
      chat_name: chat?.name ?? null,
      agent_version: this.agentVersions.get(run.agent_version_id)![1],
      created_at: NOW,
      updated_at: NOW,
    };
  }

  /** What the list says `agent`'s current version has: its skills and tools. */
  private agentUses(agent: AgentRow) {
    const skillIds = [...this.skills.values()]
      .filter((s) => s.usedBy.some((use) => use.agent === agent))
      .map((s) => s.id);
    return {
      skill_count: skillIds.length,
      tool_count: agent.tools.length,
      skill_ids: skillIds,
      tool_keys: agent.tools,
    };
  }

  private agentVersionOut(agent: AgentRow, version: number) {
    return {
      id: agent.versions[version - 1],
      version,
      note: this.agentNotes.get(agent.versions[version - 1]) ?? null,
      created_at: NOW,
      created_by: this.me,
      base_prompt: agent.base_prompt,
      model: null,
      instructions: agent.base_prompt,
      skills: [...this.skills.values()].flatMap((skill) =>
        skill.usedBy
          .filter((use) => use.agent === agent)
          .map(({ version }) => {
            const pinned = skill.versions[version - 1];
            const latest = skill.archived_at === null ? skill.versions.at(-1)! : null;
            return {
              skill_id: skill.id,
              skill_version_id: pinned.id,
              name: pinned.name,
              description: pinned.description,
              version,
              latest_version: latest?.version ?? null,
              latest_version_id: latest?.id ?? null,
              archived: skill.archived_at !== null,
            };
          }),
      ),
      tools: Object.entries(agent.toolSettings ?? {}).map(([key, config]) => {
        const tool = this.tools.find((t) => t.key === key);
        return { key, name: tool?.name ?? key, description: tool?.description ?? "", config, available: true };
      }),
      unrunnable: agent.unrunnable ?? null,
    };
  }

  agentOut(agent: AgentRow) {
    return {
      id: agent.id,
      name: agent.name,
      description: agent.description,
      created_at: NOW,
      updated_at: NOW,
      created_by: agent.created_by,
      updated_by: agent.updated_by,
      archived_at: agent.archived_at,
      archived_by: agent.archived_by,
      current_version: this.agentVersionOut(agent, agent.versions.length),
      invocable_by: agent.invocable_by ?? "everyone",
      limits: this.ownLimits(agent, TURN_LIMITS),
      limits_in_force: this.limitsInForceFor(agent, TURN_LIMITS),
    };
  }

  private nodeOut(node: NodeIn) {
    if (node.kind === "approval") {
      return {
        ...node,
        version_id: null,
        target_id: null,
        name: "Approval",
        version: null,
        latest_version: null,
        latest_version_id: null,
      };
    }
    if (node.kind === "agent") {
      const [agentId, version] = this.agentVersions.get(node.version_id!)!;
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
    const pinned = this.workflowVersions.get(node.version_id!)!;
    const target = this.workflows.get(pinned.workflow_id)!;
    return {
      ...node,
      target_id: target.id,
      name: target.name,
      version: pinned.version,
      latest_version: target.versions.length,
      latest_version_id: target.versions.at(-1)!,
    };
  }

  versionOut(version: WorkflowVersionRow) {
    return {
      id: version.id,
      version: version.version,
      note: version.note,
      created_at: version.created_at,
      created_by: this.me,
      nodes: version.nodes.map((n) => this.nodeOut(n)),
      edges: version.edges,
      input_schema: version.input_schema ?? null,
      output_schema: version.output_schema ?? null,
      unrunnable: null,
    };
  }

  workflowOut(workflow: WorkflowRow) {
    return {
      id: workflow.id,
      name: workflow.name,
      description: workflow.description,
      created_at: workflow.created_at,
      updated_at: workflow.updated_at,
      created_by: workflow.created_by,
      updated_by: workflow.updated_by,
      archived_at: workflow.archived_at,
      archived_by: workflow.archived_by,
      current_version: this.versionOut(this.current(workflow)),
      layout: workflow.layout,
      invocable_by: workflow.invocable_by ?? "everyone",
      limits: this.ownLimits(workflow, RUN_LIMITS),
      limits_in_force: this.limitsInForceFor(workflow, RUN_LIMITS),
    };
  }

  private chatSummary(chat: ChatRow) {
    return {
      id: chat.id,
      name: chat.name,
      description: chat.description,
      is_member: this.inChat(chat),
      member_count: this.liveMembers(chat).length,
      created_by: chat.created_by,
      updated_by: chat.updated_by,
      created_at: chat.created_at,
      updated_at: chat.updated_at,
      archived_at: chat.archived_at ?? null,
      archived_by: chat.archived_by ?? null,
      actions: this.chatActions(chat),
    };
  }

  /** What `me` may do to the chat, by the chat rules (`aios.api.chats.rules`), in their order. */
  private chatActions(chat: ChatRow): string[] {
    const joined = this.inChat(chat);
    const archived = Boolean(chat.archived_at);
    const admin = this.roleOf(this.me.id) === "admin";
    return [
      !archived && "post",
      !archived && !joined && "join",
      joined && "leave",
      !archived && joined && "edit",
      !archived && joined && "include",
      admin && !archived && "archive",
      admin && archived && "unarchive",
    ].filter((a): a is string => Boolean(a));
  }

  /** The agent or workflow a member is. */
  private chatTarget(kind: Kind, id: number) {
    return kind === "agent" ? this.agents.get(id) : this.workflows.get(id);
  }

  /** The chat's members as the API shows them, in the order they were added. */
  private liveMembers(chat: ChatRow): ChatMemberOut[] {
    return chat.members
      .flatMap((m): ChatMemberOut[] => {
        const { id, kind, added_at } = m;
        if (kind === "user") {
          const { user } = m;
          return [{ id, kind, target_id: user.id, name: fullName(user), added_at, archived: false }];
        }
        const target = this.chatTarget(kind, m.target_id);
        if (!target) return [];
        const archived = target.archived_at !== null;
        return [{ id, kind, target_id: target.id, name: target.name, added_at, archived }];
      })
      .sort((a, b) => a.id - b.id);
  }

  chatOut(chat: ChatRow) {
    return { ...this.chatSummary(chat), members: byKind(this.liveMembers(chat)) };
  }

  /** Who can be mentioned in a chat, as `GET /api/chats/{id}/mentionable` groups them: not its
   * archived agents and workflows, unless `archived` (posting takes a mention of one, which is
   * refused at once). */
  /** Why the signed-in member may not invoke `target` (its invocable-by, `aios.guards`), or null. */
  private invokeRefusal(kind: string, target: { name: string; invocable_by?: InvocableBy }) {
    const role = this.roleOf(this.me.id);
    const by = target.invocable_by ?? "everyone";
    if ((by === "editors" && role === "participant") || (by === "admins" && role !== "admin")) {
      return `Only ${by === "editors" ? "editors and admins" : "admins"} can invoke the ${kind} ${target.name}.`;
    }
    return null;
  }

  private mentionable(chat: ChatRow, archived = false) {
    const users = new Map(chat.members.flatMap((m) => (m.kind === "user" ? [[m.user.id, m.user]] : [])));
    return byKind(
      this.liveMembers(chat).filter((m) => archived || !m.archived).map(({ kind, target_id, name }) => {
        const user = kind === "user" ? users.get(target_id) : undefined;
        return { kind, id: target_id, name, first_name: user?.first_name ?? null, last_name: user?.last_name ?? null };
      }),
    );
  }

  private messageOut(chat: ChatRow, m: ChatMessageRow) {
    const author = m.author;
    return {
      id: m.id,
      chat_id: chat.id,
      author: isUser(author) ? { kind: "user", id: author.id, name: fullName(author) } : author,
      kind: m.kind,
      body: m.body,
      mentions: m.mentions,
      invocation_id: m.invocation_id,
      created_at: m.created_at,
    };
  }

  private invocationOut(chat: ChatRow, invocation: InvocationRow) {
    const { run_id, ...invocationRest } = invocation;
    const rest = {
      ...invocationRest,
      delivered: invocation.delivered ?? false,
      refusal_reason: invocation.refusal_reason ?? null,
      acknowledgement: invocation.delivered
        ? ""
        : this.cardOf(chat, invocation).body,
      full_output: invocation.full_output ?? null,
      summary: invocation.summary ?? null,
      handback_error: invocation.handback_error ?? null,
      delivery_error: invocation.delivery_error ?? null,
    };
    if (invocation.target_kind === "agent") {
      const run = run_id === null ? undefined : this.agentRuns.get(run_id);
      // This mention's own turn of the run, if it has one; else the run's.
      const own = run && this.turnOf(run, invocation);
      return {
        ...rest,
        workflow_run_id: null,
        agent_run_id: run ? run.id : null,
        run_deleted: invocation.started_at !== null && !run,
        workflow_run: null,
        waiting_for: this.waitingFor(invocation),
        agent_run: run
          ? {
              id: run.id,
              ...(own
                ? {
                    status: own.status,
                    started_at: own.started_at,
                    finished_at: own.finished_at,
                    tool_call_count: own.tool_calls,
                    input_tokens: own.input_tokens,
                    output_tokens: own.output_tokens,
                  }
                : {
                    status: run.status,
                    started_at: run.started_at,
                    finished_at: run.finished_at,
                    tool_call_count: run.tool_call_count,
                    input_tokens: run.input_tokens,
                    output_tokens: run.output_tokens,
                  }),
              waiting: invocation.status === "running" && this.runWaiting(run),
            }
          : null,
      };
    }
    const run = run_id === null ? undefined : this.runs.get(run_id);
    return {
      ...rest,
      workflow_run_id: run ? run.id : null,
      agent_run_id: null,
      run_deleted: invocation.started_at !== null && !run,
      workflow_run: run ? this.runOut(run) : null,
      agent_run: null,
      waiting_for: null,
    };
  }

  /** A stream's answer when it doesn't open: a 503 with a retry hint, or `gone`; else null. */
  private streamRefusal(route: Route) {
    if (this.streamsDown) {
      return route.fulfill({
        status: 503,
        headers: { "Retry-After": "1" },
        contentType: "application/json",
        body: JSON.stringify({ detail: "Live updates aren't available right now. Try again shortly." }),
      });
    }
    if (this.streamGone !== null) return this.events(route, [["gone", { reason: this.streamGone }, null]]);
    return null;
  }

  /** A batch of server-sent events, after which the stream ends (see the class comment). */
  private events(route: Route, events: [string, unknown, number | null][]) {
    const body = events
      .map(([event, data, id]) => `${id === null ? "" : `id: ${id}\n`}event: ${event}\ndata: ${JSON.stringify(data, asTheApiNamesThem)}\n\n`)
      .join("");
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache" },
      body: `retry: ${this.streamRetryMs}\n\n${body}`,
    });
  }

  /** What a chat's stream sends as it opens: what's new after `lastEventId`, the invocations still
   * going, drafts, and the chat's header if it changed. Its invocations move on a beat first. */
  private chatEvents(chat: ChatRow, lastEventId: string | null): [string, unknown, number | null][] {
    this.advanceInvocations(chat);
    const after = lastEventId === null ? null : Number(lastEventId);
    const out = this.messagesOut(chat, after === null ? [] : chat.messages.filter((m) => m.id > after));
    const events: [string, unknown, number | null][] = out.messages.map((m) => ["message", m, m.id]);
    for (const invocation of out.invocations) events.push(["invocation", invocation, null]);
    for (const [id, text] of this.drafts) {
      const invocation = this.invocations.get(id);
      if (invocation?.chat_id === chat.id) events.push(["draft", { invocation_id: id, text }, null]);
    }
    const header = {
      id: chat.id,
      name: chat.name,
      description: chat.description,
      archived_at: chat.archived_at,
      archived_by: chat.archived_by,
    };
    const seen = JSON.stringify(header);
    if (this.headersSent.has(chat.id) && this.headersSent.get(chat.id) !== seen) events.push(["chat", header, null]);
    this.headersSent.set(chat.id, seen);
    return events;
  }

  /** What the person's stream sends: each chat of the organisation's list that changed since it
   * last opened (the first opening only notes the list). */
  private listChanges(): [string, unknown, number | null][] {
    const now = new Map(
      this.organisationChats().map((c) => [
        c.id,
        JSON.stringify({ ...this.chatSummary(c), unread_count: this.unreadCount(c), newest: c.messages.at(-1)?.id ?? null }),
      ]),
    );
    const before = this.listSeen;
    this.listSeen = now;
    if (before === null) return [];
    const events: [string, unknown, number | null][] = [];
    for (const [id, summary] of now) {
      if (before.get(id) === summary) continue;
      const chat = this.chats.get(id)!;
      events.push(
        before.has(id)
          ? ["chat_activity", { organisation_id: this.organisation.id, chat_id: id, message_id: chat.messages.at(-1)?.id ?? null, unread_count: this.unreadCount(chat) }, null]
          : ["chat_list", { organisation_id: this.organisation.id, chat_id: id, change: "appeared" }, null],
      );
    }
    return events;
  }

  /** Messages as the API returns them, with the invocations they belong to and those still going. */
  private messagesOut(chat: ChatRow, messages: ChatMessageRow[]) {
    const ids = new Set(messages.map((m) => m.invocation_id));
    const invocations = [...this.invocations.values()].filter(
      (i) => i.chat_id === chat.id && (ids.has(i.id) || !["succeeded", "failed", "refused"].includes(i.status)),
    );
    return {
      messages: messages.map((m) => this.messageOut(chat, m)),
      invocations: invocations.map((i) => this.invocationOut(chat, i)),
    };
  }

  /** The page of a chat's messages a read asks for, oldest first, as the API pages them. */
  private messagesPage(chat: ChatRow, query: URLSearchParams) {
    const after = query.get("after");
    const before = query.get("before");
    let page: ChatMessageRow[];
    if (after !== null) {
      page = chat.messages.filter((m) => m.id > Number(after)).slice(0, MESSAGE_PAGE);
    } else {
      const older = before === null ? chat.messages : chat.messages.filter((m) => m.id < Number(before));
      page = older.slice(-MESSAGE_PAGE);
    }
    return this.messagesOut(chat, page);
  }

  /** The chats of the organisation `me` is in. */
  private organisationChats() {
    return [...this.chats.values()].filter((c) => c.organisation_id === this.organisation.id);
  }

  private inChat(chat: ChatRow) {
    return this.myMembership(chat) !== undefined;
  }

  private myMembership(chat: ChatRow) {
    return chat.members.find(
      (m): m is ChatUserMemberRow => m.kind === "user" && m.user.id === this.me.id,
    );
  }

  /** What `me` hasn't seen of a chat they're in: others' messages since their last read. */
  private unreadCount(chat: ChatRow) {
    const mine = this.myMembership(chat);
    if (!mine) return null;
    return chat.messages.filter(
      (m) => m.id > (mine.last_read ?? 0) && !(isUser(m.author) && m.author.id === this.me.id),
    ).length;
  }

  /** The workflows whose versions `version` runs, at any depth. */
  private contains(version: WorkflowVersionRow, found = new Set<number>()) {
    for (const n of version.nodes) {
      if (n.kind !== "workflow") continue;
      const pinned = this.workflowVersions.get(n.version_id!)!;
      found.add(pinned.workflow_id);
      this.contains(pinned, found);
    }
    return found;
  }

  /** The workflows, by name: those that aren't archived, or with `archived` those that are. */
  private live(archived = false) {
    return [...this.workflows.values()]
      .filter((w) => (w.archived_at !== null) === archived)
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  // --- Routing ---------------------------------------------------------------------------------

  /** `/api/auth/*` and `/api/dev/*`, as the API checks what it's sent: [status, body]. */
  private auth(method: string, path: string, body: Record<string, string> | null): [number, unknown] {
    const findByEmail = (email: string) =>
      this.accounts.find((a) => a.email === email.trim().toLowerCase());
    if (path === "/api/auth/options" && method === "GET") return [200, { dev_users: this.devUsers }];
    if (path.startsWith("/api/dev/") && !this.devUsers) return [404, { detail: "Not Found" }];
    if (path === "/api/dev/users" && method === "GET") {
      const sorted = [...this.accounts].sort(
        (a, b) => a.last_name.localeCompare(b.last_name) || a.first_name.localeCompare(b.first_name),
      );
      return [200, sorted.map(accountOut)];
    }
    if (path === "/api/dev/session" && method === "POST") {
      const account = this.accounts.find((a) => a.id === Number(body?.user_id));
      if (!account) return [404, { detail: "No such user" }];
      this.signedInAs = account.id;
      return [200, this.meOut(account)];
    }
    if (this.authThrottled && (path === "/api/auth/login" || path === "/api/auth/register")) {
      return [429, { detail: "Too many attempts. Try again later." }];
    }
    if (path === "/api/auth/logout" && method === "POST") {
      this.signedInAs = null;
      return [204, undefined];
    }
    if (path === "/api/auth/login" && method === "POST") {
      const account = findByEmail(body?.email ?? "");
      if (!account || account.password !== body?.password) return [401, { detail: "Email or password is incorrect" }];
      this.signedInAs = account.id;
      return [200, this.meOut(account)];
    }
    if (path === "/api/auth/register" && method === "POST" && body) {
      const [first_name, last_name, email] = [body.first_name.trim(), body.last_name.trim(), body.email.trim()];
      if (!first_name) return invalid("first_name", "String should have at least 1 character");
      if (!last_name) return invalid("last_name", "String should have at least 1 character");
      if (!isEmail(email)) return invalid("email", "The email address is not valid.");
      const refused = newPasswordInvalid("password", body.password, body.password_confirm, "password_confirm");
      if (refused) return refused;
      if (findByEmail(email)) return [409, { detail: "An account with this email already exists" }];
      const account = { id: this.id(), first_name, last_name, email: email.toLowerCase(), password: body.password };
      this.accounts.push(account);
      this.signedInAs = account.id;
      this.joinOnRegistering(account, body.invite);
      return [201, this.meOut(account)];
    }
    return [404, { detail: "Not Found" }];
  }

  /**
   * `PUT /api/me/email` and `/api/me/password`, as the API checks them: [status, body]. Changing
   * the password signs out the API's other sessions, but this one stays signed in (and the fake
   * keeps only one).
   */
  private changeCredentials(account: Account, path: string, body: Record<string, string> | null): [number, unknown] {
    if (path === "/api/me/email") {
      const email = String(body?.email ?? "").trim().toLowerCase();
      if (!isEmail(email)) return invalid("email", "The email address is not valid.");
      if (this.authThrottled) return [429, { detail: "Too many attempts. Try again later." }];
      if (body?.current_password !== account.password) return [401, { detail: "Current password is incorrect" }];
      if (this.accounts.some((a) => a.email === email && a.id !== account.id)) {
        return [409, { detail: "An account with this email already exists" }];
      }
      account.email = email;
      return [200, this.meOut(account)];
    }
    const password = String(body?.new_password ?? "");
    const refused = newPasswordInvalid("new_password", password, body?.new_password_confirm, "new_password_confirm");
    if (refused) return refused;
    if (this.authThrottled) return [429, { detail: "Too many attempts. Try again later." }];
    if (body?.current_password !== account.password) return [401, { detail: "Current password is incorrect" }];
    account.password = password;
    return [204, undefined];
  }

  async install(page: Page) {
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      (route) => this.handle(route),
    );
  }

  private async handle(route: Route) {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    let path = url.pathname;
    // As postDataJSON gives it (`any`), or a placeholder for an upload.
    let body: ReturnType<typeof request.postDataJSON> = null;
    try {
      body = request.postDataJSON?.() ?? null;
    } catch {
      // Not JSON: an upload's multipart form.
      body = "(form)";
    }
    if (method !== "GET" && !/^\/api\/o\/\d+\/chats\/\d+\/read$/.test(path)) this.writes.push([method, path, body]);
    const reply = (status: number, json?: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: json === undefined ? "" : JSON.stringify(json, asTheApiNamesThem),
      });
    const fail = (status: number, detail: string) => reply(status, { detail });
    let m: RegExpMatchArray | null;

    if (path.startsWith("/api/auth/") || path.startsWith("/api/dev/")) {
      await new Promise((resolve) => setTimeout(resolve, this.authDelay));
      const [status, json] = this.auth(method, path, body);
      return reply(status, json);
    }
    if ((m = path.match(/^\/api\/invites\/([^/]+)(\/accept)?$/))) {
      const token = decodeURIComponent(m[1]);
      // As the API: an organisation scheduled for deletion's links don't open.
      const own = this.organisation.deletion ? undefined : this.invites.find((i) => i.token === token);
      const invitation =
        this.invitations.get(token) ??
        (own && {
          organisation: { id: this.organisation.id, name: this.organisation.name },
          role: own.role ?? "editor",
          expires_at: own.expires_at,
        });
      if (!invitation) return fail(404, "This invitation link has expired or been revoked, or never existed.");
      if (!m[2] && method === "GET") return reply(200, invitation);
      if (this.signedInAs === null) return fail(401, "Not signed in");
      // As the API: a membership is added, whatever else `me` is in; one they have already is
      // answered alike, and nothing changes.
      if (invitation.organisation.id === this.organisation.id) {
        if (!this.member) {
          this.organisation.members.push({
            user: this.me,
            joined_at: new Date().toISOString(),
            invite_id: own?.id ?? null,
            role: own?.role ?? "editor",
          });
          this.member = true;
        }
        return reply(200, this.organisationOut());
      }
      const already = this.otherMemberships.find((o) => o.id === invitation.organisation.id);
      if (already) return reply(200, { ...already, creator: null, members: [], created_at: "2026-10-01T08:00:00Z", deletion: null });
      this.enter({
        ...invitation.organisation,
        slug: slugify(invitation.organisation.name),
        creator: null,
        members: [
          { user: this.me, joined_at: new Date().toISOString(), invite_id: own?.id ?? null, role: invitation.role ?? "editor" },
        ],
        created_at: "2026-10-01T08:00:00Z",
      });
      return reply(200, this.organisationOut());
    }
    const account = this.accounts.find((a) => a.id === this.signedInAs);
    if (!account) return fail(401, "Not signed in");
    if (path === "/api/me" && method === "PATCH") {
      // As the API: each name sent is trimmed and must be 1 to 100 characters; one left out is kept.
      const names: Partial<Record<"first_name" | "last_name", string>> = {};
      for (const field of ["first_name", "last_name"] as const) {
        if (body?.[field] === undefined) continue;
        const value = String(body[field] ?? "").trim();
        if (!value) return reply(422, { detail: [{ loc: ["body", field], msg: "String should have at least 1 character" }] });
        if (value.length > 100) return reply(422, { detail: [{ loc: ["body", field], msg: "String should have at most 100 characters" }] });
        names[field] = value;
      }
      Object.assign(account, names);
      return reply(200, this.meOut(account));
    }
    if ((path === "/api/me/email" || path === "/api/me/password") && method === "PUT") {
      const [status, json] = this.changeCredentials(account, path, body);
      return reply(status, json);
    }

    if (path === "/api/me") return reply(200, this.meOut(account));
    if (path === "/api/me/notifications" && method === "GET") {
      this.notificationLists++;
      return reply(200, this.notificationList());
    }
    if (path === "/api/me/notifications/read" && method === "POST") {
      for (const n of this.notifications) if (n.mine) n.read_at ??= NOW;
      return reply(204);
    }
    if ((m = path.match(/^\/api\/me\/notifications\/([\w-]+)\/read$/)) && method === "POST") {
      const notification = this.notifications.find((n) => n.id === m![1] && n.mine);
      if (!notification) return fail(404, "Notification not found");
      notification.read_at ??= NOW;
      return reply(200, this.notificationOut(notification) ?? notification);
    }
    if (path === "/api/organisations" && method === "POST") {
      const name = String(body?.name ?? "").trim();
      if (!name) return reply(422, { detail: [{ loc: ["body", "name"], msg: "String should match pattern '\\S'" }] });
      const now = new Date().toISOString();
      this.enter({
        id: this.id(),
        name,
        slug: slugify(name),
        creator: this.me,
        members: [{ user: this.me, joined_at: now, invite_id: null }],
        created_at: now,
      });
      this.invites = [];
      return reply(201, this.organisationOut());
    }
    if (path === "/api/me/stream" && method === "GET") {
      this.personStreamReads += 1;
      const refused = this.streamRefusal(route);
      if (refused) return refused;
      return this.events(route, [...this.listChanges(), ...this.notificationChanges()]);
    }
    // Everything but the person's own (signing in, the profile and notifications, invitations,
    // the tool library and timezones) is an organisation's, under its prefix (`/api/o/<id>/...`),
    // as in the API: the fake's organisation answers (as it was before the prefix, from here on),
    // and any other is a 404, as for one that doesn't exist. Nothing answers without the prefix.
    const unscoped = /^\/api\/(tools|triggers\/timezones)(\/|$)/.test(path);
    if (!unscoped) {
      const scoped = path.match(/^\/api\/o\/([^/]+)(\/.*)$/);
      if (!scoped) return fail(404, "Not Found");
      if (!this.member || scoped[1] !== String(this.organisation.id)) return fail(404, "Organisation not found");
      path = `/api${scoped[2]}`;
    }
    if (path === "/api/organisation/invites" || (m = path.match(/^\/api\/organisation\/invites\/([\w-]+)$/))) {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      if (m && method === "DELETE") {
        const id = m[1];
        if (!this.invites.some((i) => i.id === id)) return fail(404, "Not Found");
        this.invites = this.invites.filter((i) => i.id !== id);
        return reply(204);
      }
      if (method === "POST") {
        const days = Number(body?.expires_in_days ?? 7);
        if (!(days >= 1 && days <= 30)) return fail(422, "Invalid expiry");
        const n = this.id();
        const created = new Date();
        const invite: InviteRow = {
          id: `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`,
          creator: this.me,
          created_at: created.toISOString(),
          expires_at: new Date(created.getTime() + days * 86_400_000).toISOString(),
          token: `token-${n}`,
          role: (body?.role as Role | undefined) ?? "editor",
        };
        this.invites.push(invite);
        return reply(201, { ...this.inviteOut(invite), token: invite.token, url: `http://localhost/invite/${invite.token}` });
      }
      return reply(200, this.invites.map((i) => this.inviteOut(i)));
    }
    if (path === "/api/organisation/leave" && method === "POST") {
      if (this.organisation.members.length <= 1) {
        return fail(409, "You're its only member, so nobody would be left to see what's in it. Delete the organisation instead.");
      }
      const admins = this.organisation.members.filter((m) => this.roleOf(m.user.id) === "admin");
      if (admins.length === 1 && admins[0].user.id === this.me.id) {
        return fail(409, "You're its only admin, and an organisation needs an admin, so you can't leave while others are still members. Make someone else an admin first.");
      }
      // Only the membership goes: `me` is left in the others they're in, or in none.
      this.organisation.members = this.organisation.members.filter((m) => m.user.id !== this.me.id);
      this.member = false;
      return reply(200, this.meOut(account));
    }
    if (path === "/api/organisation/limits" && method === "PUT") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      this.limits = { ...this.limits, ...body };
      return reply(200, this.organisationOut());
    }
    if (path === "/api/usage" && method === "GET") {
      if (this.roleOf(this.me.id) !== "admin") {
        const who = this.roleOf(this.me.id) === "editor" ? "Editors" : "Participants";
        return fail(403, `${who} can't read usage totals. Ask an admin.`);
      }
      this.usageReads.push(url.search);
      const group = url.searchParams.get("group") ?? "agent";
      return reply(200, {
        currency: "GBP",
        group,
        start: url.searchParams.get("start") ?? "2026-09-15",
        end: url.searchParams.get("end") ?? "2026-10-14",
        totals: this.usage[group] ?? [],
      });
    }
    if (path === "/api/organisation/budget") {
      if (method === "GET") return reply(200, this.budgetOut());
      if (method === "PUT") {
        const refused = this.refuseUnlessAdmin();
        if (refused) return fail(403, refused);
        const amount = (v: unknown) => v === null || v === undefined || (typeof v === "number" && v >= 0);
        const allowances = (body?.role_allowances ?? {}) as Record<string, unknown>;
        if (!amount(body?.budget) || !Object.values(allowances).every(amount)) return fail(422, "Invalid amount");
        this.budget.budget = (body.budget as number | null) ?? null;
        this.budget.role_allowances = {
          admin: (allowances.admin as number | null) ?? null,
          editor: (allowances.editor as number | null) ?? null,
          participant: (allowances.participant as number | null) ?? null,
        };
        return reply(200, this.budgetOut());
      }
    }
    if ((m = path.match(/^\/api\/organisation\/members\/(\d+)\/allowance$/)) && method === "PUT") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      const userId = Number(m[1]);
      if (!this.organisation.members.some((x) => x.user.id === userId)) {
        return fail(404, "They aren't a member of this organisation.");
      }
      const value = body?.allowance;
      if (!(value === null || (typeof value === "number" && value >= 0))) return fail(422, "Invalid allowance");
      this.budget.allowances[userId] = value as number | null;
      return reply(200, this.budgetOut());
    }
    if (path === "/api/organisation/insights-people" && method === "PUT") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      if (typeof body?.visible !== "boolean") return fail(422, "Invalid visible");
      this.insightsPeopleVisible = body.visible;
      return reply(200, this.organisationOut());
    }
    if (path === "/api/insights/ask" && method === "POST") {
      const question = typeof body?.question === "string" ? body.question.trim() : "";
      if (!question) return fail(422, "Ask a question.");
      this.chartQuestions.push(question);
      const answer = this.chartAnswers.shift();
      if (!answer) return fail(502, "The insights agent couldn't answer: no answer scripted");
      if ("status" in answer) return fail(answer.status, answer.detail);
      return reply(200, {
        question,
        title: answer.title,
        sql: answer.sql,
        spec: answer.spec,
        columns: answer.data ? Object.keys(answer.data[0] ?? {}) : null,
        data: answer.error ? null : (answer.data ?? []),
        truncated: answer.truncated ?? false,
        error: answer.error ?? null,
      });
    }
    const chartOut = (c: FakeApi["savedCharts"][number]) => ({
      ...c,
      can_change: this.roleOf(this.me.id) === "admin" || c.created_by?.id === this.me.id,
    });
    if (path === "/api/insights/charts" && method === "GET") {
      return reply(200, this.savedCharts.map(chartOut));
    }
    if (path === "/api/insights/charts" && method === "POST") {
      if (!body?.title?.trim() || !body?.sql || typeof body?.spec !== "object") return fail(422, "Invalid chart");
      const now = new Date().toISOString();
      const chart = {
        id: Math.max(0, ...this.savedCharts.map((c) => c.id)) + 1,
        title: body.title.trim(),
        question: body.question ?? "",
        sql: body.sql,
        spec: body.spec,
        created_by: { id: this.me.id, name: fullName(this.me) },
        created_at: now,
        updated_at: now,
      };
      this.savedCharts.unshift(chart);
      return reply(201, chartOut(chart));
    }
    if ((m = path.match(/^\/api\/insights\/charts\/(\d+)(\/data)?$/))) {
      const id = Number(m[1]);
      const chart = this.savedCharts.find((c) => c.id === id);
      if (!chart) return fail(404, "No such chart.");
      if (m[2] && method === "GET") {
        const rows = this.chartRows[id] ?? { data: [] };
        if ("broken" in rows) return fail(409, `This chart's query no longer works: ${rows.broken}`);
        return reply(200, { columns: Object.keys(rows.data[0] ?? {}), data: rows.data, truncated: rows.truncated ?? false });
      }
      if (!chartOut(chart).can_change) return fail(403, "Only the chart's creator or an admin can change it.");
      if (method === "PATCH") {
        if (!body?.title?.trim()) return fail(422, "Give the chart a title.");
        chart.title = body.title.trim();
        chart.updated_at = new Date().toISOString();
        return reply(200, chartOut(chart));
      }
      if (method === "DELETE") {
        this.savedCharts = this.savedCharts.filter((c) => c.id !== id);
        return reply(204);
      }
    }
    if (path.startsWith("/api/insights/") && method === "GET") {
      const panel = path.slice("/api/insights/".length) as keyof InsightsData;
      if (!(panel in this.insights)) return fail(404, "Not Found");
      this.insightsReads.push(`${panel}${url.search}`);
      const group = url.searchParams.get("group") ?? "agent";
      if (group === "person" && this.roleOf(this.me.id) !== "admin" && !this.insightsPeopleVisible) {
        return fail(
          403,
          "Usage by person is shown to admins only. An admin can show it to everyone in the organisation's settings.",
        );
      }
      const range = { start: url.searchParams.get("start") ?? "2026-10-14", end: url.searchParams.get("end") ?? "2026-11-12" };
      const data = this.insights[panel];
      if (panel === "activity" || panel === "usage") {
        const grouped = (data as Record<string, object>)[group] ?? {};
        return reply(200, { ...range, group, ...(panel === "usage" ? { currency: "GBP" } : {}), ...grouped });
      }
      return reply(200, panel === "sources" ? data : { ...range, ...data });
    }
    if (path === "/api/organisation/approval-expiry" && method === "PUT") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      const days = body?.days;
      if (!Number.isInteger(days) || days < 1 || days > 365) return fail(422, "Invalid days");
      this.approvalExpiryDays = days;
      return reply(200, this.organisationOut());
    }
    if (path === "/api/organisation/retention" && method === "PUT") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      const valid = (d: unknown) => d === null || (Number.isInteger(d) && (d as number) >= 1 && (d as number) <= 36_500);
      if (!valid(body?.run_days) || !valid(body?.chat_days)) return fail(422, "Invalid retention");
      this.retention = { run_days: body.run_days, chat_days: body.chat_days };
      return reply(200, this.organisationOut());
    }
    if (path === "/api/organisation/deletion") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      if (method === "POST") {
        if (String(body?.name ?? "").trim() !== this.organisation.name) return fail(422, "That isn't the organisation's name.");
        if (this.organisation.deletion) return fail(409, "The organisation is already scheduled for deletion.");
        const now = new Date();
        this.organisation.deletion = {
          scheduled_at: now.toISOString(),
          scheduled_by: this.me,
          deletes_at: new Date(now.getTime() + this.deletionGraceDays * 86_400_000).toISOString(),
        };
      } else if (method === "DELETE") {
        if (!this.organisation.deletion) return fail(409, "The organisation isn't scheduled for deletion.");
        this.organisation.deletion = null;
      }
      return reply(200, this.organisationOut());
    }
    if ((m = path.match(/^\/api\/organisation\/members\/(\d+)\/role$/)) && method === "PUT") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      const row = this.organisation.members.find((r) => r.user.id === Number(m![1]));
      if (!row) return fail(404, "They aren't a member of this organisation.");
      const role = body?.role as Role;
      const admins = this.organisation.members.filter((r) => this.roleOf(r.user.id) === "admin");
      if (role !== "admin" && admins.length === 1 && admins[0] === row) {
        return fail(409, "An organisation needs an admin");
      }
      row.role = role;
      return reply(200, this.organisationOut());
    }
    if (path === "/api/organisation") {
      if (method === "PUT") {
        const refused = this.refuseUnlessAdmin();
        if (refused) return fail(403, refused);
        const name = String(body?.name ?? "").trim();
        if (!name) return fail(422, "Invalid name");
        this.organisation.name = name;
      }
      return reply(200, this.organisationOut());
    }
    if (path === "/api/data-store") {
      await new Promise((resolve) => setTimeout(resolve, this.dataStoreDelay));
      if (this.dataStoreFails) return fail(500, "Internal Server Error");
      return reply(200, { status: this.dataStoreStatus, usage: this.dataStoreUsage });
    }
    if (path === "/api/documents" && method === "GET") this.documentLists++;
    if (
      path === "/api/documents" ||
      path.startsWith("/api/documents/") ||
      path === "/api/sources" ||
      path.startsWith("/api/sources/") ||
      path === "/api/source-providers" ||
      path === "/api/source-kinds" ||
      path === "/api/connector-providers" ||
      path === "/api/connectors" ||
      path.startsWith("/api/connectors/") ||
      path === "/api/search" ||
      path.startsWith("/api/data/")
    ) {
      if (this.dataStoreStatus !== "ready") {
        return reply(409, {
          detail:
            this.dataStoreStatus === "failed"
              ? "The organisation's data store couldn't be set up. We'll try again; please check back later."
              : "The organisation's data store is being set up. Try again in a moment.",
          data_store_status: this.dataStoreStatus,
        });
      }
      if (this.dataStoreBusy) return fail(503, "The organisation's data store is busy. Try again in a moment.");
      if (path.startsWith("/api/data/")) {
        if (this.tablesFail) return fail(500, "Internal Server Error");
        const [status, json] = this.tablesResponse(path, new URL(request.url()).searchParams, method, body);
        return reply(status, json);
      }
      if (path === "/api/search") {
        const params = new URL(request.url()).searchParams;
        this.searches.push(params.toString());
        const q = params.get("q")?.toLowerCase() ?? "";
        const kinds = params.getAll("kind");
        const passages: SearchItemRow[] = this.documents
          .filter(
            (d) =>
              d.status === "ready" &&
              (d.verdict === "clean" || d.verdict === "released") &&
              d.filename.toLowerCase().includes(q),
          )
          .map((d) => ({
            id: d.id * 1000,
            kind: "document_chunk",
            text: `A passage from ${d.filename}.`,
            created_at: d.created_at,
            ref: {},
            document_id: d.id,
            filename: d.filename,
          }));
        const items = [...passages, ...this.searchItems.filter((i) => i.text.toLowerCase().includes(q))]
          .filter((i) => kinds.length === 0 || kinds.includes(i.kind))
          .slice(0, Number(params.get("limit") ?? 8));
        return reply(
          200,
          items.map((item, i) => ({
            document_id: null,
            filename: null,
            chat_id: null,
            chat_name: null,
            message_id: null,
            agent_id: null,
            agent_name: null,
            agent_run_id: null,
            run_title: null,
            ...item,
            score: 0.03 - i * 0.001,
            semantic_rank: i + 1,
            lexical_rank: i % 2 === 0 ? i + 1 : null,
          })),
        );
      }
      if (path === "/api/sources" && method === "GET") {
        return reply(200, this.sources.map((s) => this.sourceOut(s)));
      }
      if (path === "/api/source-providers" && method === "GET") return reply(200, this.sourceProviders);
      if (path === "/api/source-kinds" && method === "GET") return reply(200, this.sourceKinds);
      const notAdmin = () => {
        const who = this.roleOf(this.me.id) === "editor" ? "Editors" : "Participants";
        return fail(403, `${who} can't add, change, sync, pause or remove sources. Ask an admin.`);
      };
      if (path === "/api/connector-providers" && method === "GET") {
        return reply(
          200,
          this.connectorProviders.map((p) => ({
            ...p,
            connector_id: this.connectors.find((c) => c.provider === p.provider)?.id ?? null,
          })),
        );
      }
      if (path === "/api/connectors" && method === "GET") {
        return reply(200, this.connectors.map((c) => this.connectorOut(c)));
      }
      if (path === "/api/connectors" && method === "POST") {
        if (this.roleOf(this.me.id) !== "admin") return notAdmin();
        const provider = this.connectorProviders.find((p) => p.provider === body.provider);
        if (!provider || provider.unavailable) return fail(409, provider?.unavailable ?? "There's no such provider.");
        if (this.connectors.some((c) => c.provider === provider.provider)) {
          return fail(409, `This organisation already has a ${provider.title} connector: remove it to connect another.`);
        }
        const connector = this.addConnector(provider.provider);
        return reply(200, { redirect_url: inOrg(`/data/connectors/${connector.id}`) });
      }
      if ((m = path.match(/^\/api\/connectors\/(\d+)(\/[\w-]+)?$/))) {
        const connector = this.connectors.find((c) => c.id === Number(m![1]));
        if (!connector) return fail(404, "Not Found");
        const action = m[2] ?? "";
        if (action === "" && method === "GET") return reply(200, this.connectorOut(connector));
        if (action === "/tools" && method === "GET") {
          if (connector.status === "needs_reauth") return fail(409, "This connector needs reconnecting first.");
          return reply(200, this.connectorTools[connector.provider] ?? []);
        }
        if (this.roleOf(this.me.id) !== "admin") return notAdmin();
        if (action === "/reconnect" && method === "POST") {
          connector.status = "active";
          const source = this.sources.find((s) => s.id === connector.source_id);
          if (source) {
            source.status = "active";
            source.health = "healthy";
          }
          return reply(200, { redirect_url: inOrg(`/data/connectors/${connector.id}`) });
        }
        if (action === "/scope-options" && method === "GET") {
          if (connector.status === "needs_reauth") return fail(409, "This connector needs reconnecting first.");
          const q = (new URL(request.url()).searchParams.get("q") ?? "").toLowerCase();
          return reply(200, this.scopeOptions.filter((o) => o.name.toLowerCase().includes(q)));
        }
        if (action === "/scope" && method === "PUT") {
          const items = (body.items as string[]) ?? [];
          const acknowledged = Boolean(body.acknowledge_restricted);
          const picked = items.map((id) => this.scopeOptions.find((o) => o.id === id)!);
          if (picked.some((o) => o.restricted) && !acknowledged) return fail(422, RESTRICTED_WARNING);
          this.scopeSaves.push({ id: connector.id, items, acknowledged });
          connector.scope = picked;
          if (connector.source_id === null) {
            connector.source_id = this.addSource(connector.name, "connector", {
              provider: connector.provider,
              syncs: [this.sync({ kind: "full" })],
            }).id;
          }
          return reply(200, this.connectorOut(connector));
        }
        if (action === "" && method === "DELETE") {
          const keep = new URL(request.url()).searchParams.get("keep") === "true";
          this.connectorRemovals.push({ id: connector.id, keep });
          this.connectors = this.connectors.filter((c) => c !== connector);
          this.sources = this.sources.filter((s) => s.id !== connector.source_id);
          return reply(202, null);
        }
        return fail(404, "Not Found");
      }
      const tooOften = (seconds: unknown) =>
        typeof seconds === "number" && seconds < 300
          ? fail(422, "A source can sync at most every 5 minutes (300 seconds).")
          : null;
      // As `aios.url_fetch` refuses: not HTTP(S) on 80 or 443, or a private, loopback or
      // link-local address.
      const unreachable = (url: string) => {
        try {
          const parsed = new URL(url);
          return (
            !["http:", "https:"].includes(parsed.protocol) ||
            !["", "80", "443"].includes(parsed.port) ||
            /^(localhost|127\.|10\.|192\.168\.|169\.254\.|\[)/.test(parsed.hostname)
          );
        } catch {
          return true;
        }
      };
      if (path === "/api/sources" && method === "POST" && body.kind && body.kind !== "connector") {
        if (this.roleOf(this.me.id) !== "admin") return notAdmin();
        const kind = body.kind as "url" | "webhook" | "email";
        const offered = this.sourceKinds.find((k) => k.kind === kind);
        if (offered?.unavailable) return fail(422, offered.unavailable);
        if (kind === "url") {
          if (unreachable(String(body.url ?? ""))) return fail(422, "This address isn't reachable from aios");
          const refused = tooOften(body.interval_seconds);
          if (refused) return refused;
          const source = this.addSource(String(body.name), "url", {
            url: String(body.url),
            interval_seconds: (body.interval_seconds as number | undefined) ?? 900,
            syncs: [this.sync({ kind: "full" })],
          });
          return reply(201, this.sourceDetail(source));
        }
        const source = this.addSource(String(body.name), kind, {
          interval_seconds: null,
          ...(kind === "email" ? { allowed_senders: (body.allowed_senders as string[] | undefined) ?? [] } : {}),
        });
        return reply(201, this.rotate(source));
      }
      if ((m = path.match(/^\/api\/sources\/(\d+)\/secret$/)) && method === "POST") {
        const source = this.sources.find((s) => s.id === Number(m![1]));
        if (!source) return fail(404, "Not Found");
        if (this.roleOf(this.me.id) !== "admin") return notAdmin();
        if (source.kind !== "webhook" && source.kind !== "email") {
          return fail(409, "Only a webhook or email source has a secret or address to rotate.");
        }
        return reply(200, this.rotate(source));
      }
      if (path === "/api/sources" && method === "POST") {
        if (this.roleOf(this.me.id) !== "admin") return notAdmin();
        const provider = this.sourceProviders.find((p) => p.provider === body.provider);
        if (!provider || provider.unavailable) return fail(422, provider?.unavailable ?? "There's no such provider.");
        const refused = tooOften(body.interval_seconds);
        if (refused) return refused;
        const source = this.addSource(String(body.name), "connector", {
          provider: String(body.provider),
          interval_seconds: (body.interval_seconds as number | undefined) ?? 900,
          syncs: [this.sync({ kind: "full" })],
        });
        return reply(201, this.sourceDetail(source));
      }
      if ((m = path.match(/^\/api\/sources\/(\d+)\/sync$/)) && method === "POST") {
        const source = this.sources.find((s) => s.id === Number(m![1]));
        if (!source) return fail(404, "Not Found");
        if (this.roleOf(this.me.id) !== "admin") return notAdmin();
        if (source.kind === "upload") {
          return fail(
            409,
            "Uploads can't be paused or removed: it's where every uploaded document goes, so uploading is always on.",
          );
        }
        if (source.status === "paused") return fail(409, "This source is paused. Resume it to sync it.");
        if (source.syncs?.some((x) => x.status === "running")) {
          return fail(409, "This source is syncing now. Try again once it's finished.");
        }
        const full = Boolean(body?.full);
        this.syncRequests.push({ id: source.id, full });
        source.syncs = [this.sync({ kind: full ? "full" : "manual" }), ...(source.syncs ?? [])];
        return reply(202, null);
      }
      if ((m = path.match(/^\/api\/sources\/(\d+)$/))) {
        const source = this.sources.find((s) => s.id === Number(m![1]));
        if (!source) return fail(404, "Not Found");
        if (method === "GET") return reply(200, this.sourceDetail(source));
        if (this.roleOf(this.me.id) !== "admin") return notAdmin();
        if (source.kind === "upload") {
          return fail(
            409,
            "Uploads can't be paused or removed: it's where every uploaded document goes, so uploading is always on.",
          );
        }
        if (method === "PATCH") {
          const refused = tooOften(body.interval_seconds);
          if (refused) return refused;
          if (body.paused === true) source.status = "paused";
          if (body.paused === false && source.status === "paused") source.status = "active";
          if (typeof body.name === "string") source.name = body.name;
          if (typeof body.interval_seconds === "number") source.interval_seconds = body.interval_seconds;
          if (Array.isArray(body.allowed_senders)) {
            source.allowed_senders = (body.allowed_senders as string[]).map((x) => x.toLowerCase());
          }
          if (typeof body.url === "string") {
            if (unreachable(body.url)) return fail(422, "This address isn't reachable from aios");
            source.url = body.url;
          }
          return reply(200, this.sourceDetail(source));
        }
        return fail(404, "Not Found");
      }
      if ((m = path.match(/^\/api\/documents\/(\d+)$/)) && method === "DELETE") {
        const synced = this.documents.find((d) => d.id === Number(m![1]) && d.source.kind !== "upload");
        if (synced) {
          return fail(
            409,
            `This document is synced from ${synced.source.name}: remove it at its source, and its next sync removes it here. Deleted here, it would come back.`,
          );
        }
        const before = this.documents.length;
        this.documents = this.documents.filter((d) => d.id !== Number(m![1]));
        if (this.documents.length === before) return fail(404, "Not Found");
        this.recordFiles();
        return reply(204);
      }
      if (path === "/api/documents" && method === "POST") {
        const filename = /filename="([^"]+)"/.exec(request.postData() ?? "")?.[1] ?? "upload";
        await this.uploadGate?.wait;
        if (this.tooLarge.has(filename)) return reply(413);
        const { db_bytes, db_quota_bytes, files_bytes, files_quota_bytes } = this.dataStoreUsage;
        const gb = (n: number) => `${(n / 1024 ** 3).toFixed(1)} GB`;
        if (db_bytes !== null && db_bytes >= db_quota_bytes) {
          return fail(
            507,
            `The organisation's data store is full: its data takes up ${gb(db_bytes)} of the` +
              ` ${gb(db_quota_bytes)} allowed, so documents can't be uploaded.`,
          );
        }
        if (files_bytes !== null && files_bytes >= files_quota_bytes) {
          return fail(
            507,
            `The organisation's data store is full: its documents use ${gb(files_bytes)} of the` +
              ` ${gb(files_quota_bytes)} allowed. Delete some to make room.`,
          );
        }
        const doc = this.addDocument(filename, {
          uploaded_by: this.me,
          ...(this.ingestAs === "pending" ? { status: "pending", chunk_count: 0 } : {}),
        });
        this.recordFiles();
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
    if (path === "/api/approvals" && method === "GET") {
      const kind = url.searchParams.get("kind");
      const status = url.searchParams.get("status");
      const shown = this.schemaChanges.filter(
        (r) =>
          (!kind || (r.kind ?? "schema_change") === kind) && (!status || r.status === status),
      );
      const pending = shown.filter((r) => r.status === "pending");
      const decided = shown.filter((r) => r.status !== "pending").reverse();
      return reply(
        200,
        [...pending, ...decided].map((r) => this.schemaChangeOut(r)),
      );
    }
    if ((m = path.match(/^\/api\/approvals\/([\w-]+)\/impact$/)) && method === "GET") {
      const request = this.schemaChanges.find((r) => r.id === m![1]);
      if (!request || !request.decider) return fail(404, "Approval not found");
      if (request.kind === "workflow_step") return fail(422, "Only a schema change has an impact.");
      return reply(200, { removes: request.impact });
    }
    if ((m = path.match(/^\/api\/approvals\/([\w-]+)$/)) && method === "GET") {
      const request = this.schemaChanges.find((r) => r.id === m![1]);
      if (!request || !request.decider) return fail(404, "Approval not found");
      return reply(200, this.schemaChangeOut(request));
    }
    if ((m = path.match(/^\/api\/approvals\/([\w-]+)\/(approve|reject)$/)) && method === "POST") {
      const request = this.schemaChanges.find((r) => r.id === m![1]);
      if (!request || !request.decider) return fail(404, "Approval not found");
      if (request.status !== "pending") {
        return fail(409, `This approval has already been ${request.status}`);
      }
      if (!this.decides(request.needs_role)) {
        const why =
          request.kind === "workflow_step"
            ? "its workflow's approval step"
            : "the organisation's approval rules";
        return fail(
          403,
          `This approval needs ${request.needs_role === "admin" ? "an admin" : "an editor"} to approve or reject it (${why}). Ask one.`,
        );
      }
      const typed = (body?.confirm ?? "").trim();
      if (m[2] === "approve" && request.confirm_with && typed !== request.confirm_with) {
        return fail(422, `This request drops data: Type \`${request.confirm_with}\` to confirm.`);
      }
      request.decided_at = NOW;
      const name = `${this.me.first_name} ${this.me.last_name}`;
      request.decided_by = { id: this.me.id, name, former_member: false };
      request.note = (body?.note ?? "").trim() || null;
      request.status = m[2] === "approve" ? "approved" : "rejected";
      this.clearApprovalNotifications(request.id);
      return reply(200, this.schemaChangeOut(request));
    }
    if (path === "/api/jev-status" && method === "GET") {
      return reply(200, { up: this.jevUp });
    }
    if (path === "/api/flagged" || path.startsWith("/api/flagged/")) {
      if (this.roleOf(this.me.id) !== "admin") {
        const role = this.roleOf(this.me.id) === "editor" ? "Editors" : "Participants";
        return fail(403, `${role} can't see flagged content or release it. Ask an admin.`);
      }
      if (path === "/api/flagged/synced-items" && method === "GET") {
        return reply(
          200,
          this.flaggedItems.filter((i) => !i.released).map(({ released: _, ...item }) => item),
        );
      }
      if ((m = path.match(/^\/api\/flagged\/synced-items\/(\d+)\/release$/)) && method === "POST") {
        const item = this.flaggedItems.find((i) => i.id === Number(m![1]));
        if (!item) return fail(404, "Not Found");
        if (item.released) return fail(409, "Only a flagged item can be released.");
        item.released = true;
        return reply(200, { id: item.id, verdict: "released" });
      }
      if (path === "/api/flagged" && method === "GET") {
        return reply(
          200,
          this.documents
            .filter((d) => d.verdict === "flagged")
            .map((d) => ({
              id: d.id,
              filename: d.filename,
              reason: d.verdict_reason,
              checked_at: d.created_at,
              uploaded_by: d.uploaded_by,
              created_at: d.created_at,
            })),
        );
      }
      if ((m = path.match(/^\/api\/flagged\/(\d+)\/release$/)) && method === "POST") {
        const doc = this.documents.find((d) => d.id === Number(m![1]));
        if (!doc) return fail(404, "Not Found");
        if (doc.verdict !== "flagged") return fail(409, "Only a flagged document can be released.");
        doc.verdict = "released";
        this.auditEntries.unshift({
          id: this.id(),
          at: NOW,
          actor: { kind: "person", user: { id: this.me.id, name: `${this.me.first_name} ${this.me.last_name}` }, agent: null, run_id: null },
          action: "document.released",
          target_kind: "document",
          target_id: String(doc.id),
          details: { filename: doc.filename, reason: doc.verdict_reason },
        });
        return reply(200, { id: doc.id, verdict: doc.verdict });
      }
      return fail(404, "Not Found");
    }
    if (path === "/api/audit-log" && method === "GET") {
      if (this.roleOf(this.me.id) !== "admin") {
        const role = this.roleOf(this.me.id) === "editor" ? "Editors" : "Participants";
        return fail(403, `${role} can't read the audit log. Ask an admin.`);
      }
      const q = url.searchParams;
      const actor = q.get("actor");
      const action = q.get("action");
      const since = q.get("since");
      const until = q.get("until");
      const entries = this.auditEntries.filter(
        (e) =>
          (actor === null || e.actor.user?.id === Number(actor)) &&
          (action === null || e.action === action || e.action.startsWith(`${action}.`)) &&
          (since === null || e.at.slice(0, 10) >= since) &&
          (until === null || e.at.slice(0, 10) <= until),
      );
      return reply(200, { entries, next: null });
    }
    if (path === "/api/approval-rules") {
      if (method === "PUT") return fail(405, "Method Not Allowed");
      return reply(200, this.approvalRulesOut());
    }
    if ((m = path.match(/^\/api\/approval-rules\/(\w+)$/)) && method === "PUT") {
      const refused = this.refuseUnlessAdmin();
      if (refused) return fail(403, refused);
      const kind = m[1];
      if (!(kind in DEFAULT_RULES)) return fail(404, "There's no such kind of change");
      const role = body?.role as Role;
      if (DEFAULT_RULES[kind] === "admin" && role !== "admin") {
        return fail(422, "It needs at least an admin to approve: a rule can be raised, but not lowered below its default.");
      }
      if (role === "participant") return fail(422, "It needs at least an editor to approve.");
      this.approvalRules[kind] = role;
      return reply(200, this.approvalRulesOut());
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
      else if (!item.stallRestore) this.dropped = this.dropped.filter((d) => d !== item);
      return reply(202, { ...out, blocked: null });
    }
    if (path === "/api/tools") return reply(200, this.tools);
    if (path === "/api/skills" && method === "GET") {
      const archived = url.searchParams.get("archived") === "true";
      return reply(
        200,
        [...this.skills.values()].filter((s) => (s.archived_at !== null) === archived).map((s) => ({
          id: s.id,
          name: s.name,
          description: s.versions.at(-1)!.description,
          version: s.versions.length,
          version_id: s.versions.at(-1)!.id,
          updated_at: s.updated_at ?? NOW,
          agent_count: new Set(
            s.usedBy.filter(({ agent }) => this.agents.has(agent.id)).map(({ agent }) => agent.id),
          ).size,
          created_by: this.me,
          updated_by: this.me,
          archived_at: s.archived_at,
          archived_by: s.archived_by,
        })),
      );
    }
    if ((m = path.match(/^\/api\/skills\/(\d+)\/(archive|unarchive)$/)) && method === "POST") {
      const skill = this.skills.get(Number(m[1]));
      if (!skill) return fail(404, "Not Found");
      if (m[2] === "unarchive") {
        skill.archived_at = null;
        skill.archived_by = null;
      } else if (skill.archived_at === null) {
        skill.archived_at = new Date().toISOString();
        skill.archived_by = this.me;
      }
      return reply(200, this.skillOut(skill));
    }
    if ((m = path.match(/^\/api\/skills\/(\d+)\/used-by$/)) && method === "GET") {
      const skill = this.skills.get(Number(m[1]));
      if (!skill) return fail(404, "Not Found");
      return reply(200, { agents: this.skillUsage(skill) });
    }
    if ((m = path.match(/^\/api\/skills\/(\d+)\/versions\/(\d+)\/restore$/)) && method === "POST") {
      const skill = this.skills.get(Number(m[1]));
      const from = skill?.versions[Number(m[2]) - 1];
      if (!skill || !from) return fail(404, "Not Found");
      skill.versions.push({
        ...from,
        id: this.id(),
        version: skill.versions.length + 1,
        note: `Restored from v${from.version}`,
      });
      return reply(200, this.skillOut(skill));
    }
    if ((m = path.match(/^\/api\/skills\/(\d+)(\/.*)?$/)) && method === "GET") {
      const skill = this.skills.get(Number(m[1]));
      const rest = m[2] ?? "";
      if (!skill) return fail(404, "Not Found");
      if (rest === "") return reply(200, this.skillOut(skill));
      if (rest === "/versions") {
        return reply(
          200,
          skill.versions
            .map(({ id, version, note }) => ({ id, version, note, created_at: NOW }))
            .reverse(),
        );
      }
      if ((m = rest.match(/^\/versions\/(\d+)$/))) {
        const version = skill.versions[Number(m[1]) - 1];
        if (!version) return fail(404, "Not Found");
        return reply(200, { ...version, created_at: NOW });
      }
    }
    if (path === "/api/agents") {
      const archived = url.searchParams.get("archived") === "true";
      return reply(
        200,
        [...this.agents.values()].filter((a) => (a.archived_at !== null) === archived).map((a) => ({
          id: a.id,
          name: a.name,
          description: a.description,
          version: a.versions.length,
          version_id: a.versions.at(-1),
          ...this.agentUses(a),
          run_count: this.agentRunsOf(a).length,
          updated_at: NOW,
          created_by: a.created_by,
          updated_by: a.updated_by,
          archived_at: a.archived_at,
          archived_by: a.archived_by,
        })),
      );
    }
    if (path === "/api/imports" && method === "POST") {
      const refused = this.refuseUnlessBuilder();
      if (refused) return fail(403, refused);
      const filename = /filename="([^"]+)"/.exec(request.postData() ?? "")?.[1] ?? "upload";
      const dryRun = url.searchParams.get("dry_run") === "true";
      this.imports.push({ filename, dryRun });
      if (!this.nextImport) return fail(422, "That file isn't an export: format: Field required");
      const items = this.nextImport.map((i, n) => {
        const name = i.imported_name ?? i.name;
        let id: number | null = null;
        if (!dryRun && i.kind === "agent") {
          const agent = this.addAgent(name);
          agent.unrunnable = i.unrunnable ?? null;
          id = agent.id;
        }
        return {
          kind: i.kind,
          ref: `${i.kind}-${n + 1}`,
          name: i.name,
          imported_name: name,
          renamed: name !== i.name,
          unrunnable: i.unrunnable ?? null,
          id,
        };
      });
      return reply(200, { dry_run: dryRun, root: items.at(-1), items });
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)$/)) && method === "PUT") {
      const agent = this.agents.get(Number(m[1]));
      if (!agent) return fail(404, "Not Found");
      const refused = this.refuseUnlessBuilder() ?? this.applyGuards(agent, body);
      if (refused) return fail(403, refused);
      agent.name = body.name;
      agent.description = body.description ?? "";
      agent.updated_by = this.me;
      return reply(200, this.agentOut(agent));
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)\/(archive|unarchive)$/)) && method === "POST") {
      const agent = this.agents.get(Number(m[1]));
      if (!agent) return fail(404, "Not Found");
      if (m[2] === "unarchive") {
        agent.archived_at = null;
        agent.archived_by = null;
      } else if (agent.archived_at === null) {
        agent.archived_at = new Date().toISOString();
        agent.archived_by = this.me;
        for (const t of this.schedules) {
          if (t.target_kind === "agent" && t.target_id === agent.id) t.enabled = false;
        }
      }
      return reply(200, this.agentOut(agent));
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)\/used-by$/)) && method === "GET") {
      const agent = this.agents.get(Number(m[1]));
      if (!agent) return fail(404, "Not Found");
      const { workflows, triggers } = this.agentUsedBy.get(agent.id) ?? { workflows: [], triggers: [] };
      const chats = [...this.chats.values()]
        .filter((c) => c.members.some((x) => x.kind === "agent" && x.target_id === agent.id))
        .map((c) => ({ id: c.id, name: c.name }));
      const tables = this.dataTables.filter((t) => t.owner === agent.id).map((t) => ({ name: t.name }));
      return reply(200, { workflows, triggers, chats, tables });
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)\/memories$/)) && method === "GET") {
      const agentId = Number(m[1]);
      if (!this.agents.has(agentId)) return fail(404, "Not Found");
      const listed = [...this.memories.values()]
        .filter((x) => x.agent_id === agentId)
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id)
        .map(({ agent_id: _, ...rest }) => ({ ...rest, verdict: "derived" }));
      return reply(200, listed);
    }
    if ((m = path.match(/^\/api\/agents\/(\d+)\/memories\/(\d+)$/)) && (method === "PATCH" || method === "DELETE")) {
      const refusal = this.refuseUnlessBuilder();
      if (refusal) return fail(403, refusal);
      const memory = this.memories.get(Number(m[2]));
      if (!memory || memory.agent_id !== Number(m[1])) return fail(404, "Not Found");
      if (method === "DELETE") {
        this.memories.delete(memory.id);
        this.addAuditEntry({ action: "memory.deleted", target_kind: "memory", target_id: String(memory.id) });
        return reply(204);
      }
      const content = String(body?.content ?? "").trim();
      if (!content) return fail(422, "There's nothing to remember: the text is blank.");
      memory.content = content;
      memory.updated_at = new Date().toISOString();
      memory.updated_by = this.me;
      this.addAuditEntry({ action: "memory.edited", target_kind: "memory", target_id: String(memory.id) });
      const { agent_id: _, ...rest } = memory;
      return reply(200, { ...rest, verdict: "derived" });
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
      agent.updated_by = this.me;
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
          agent_name: agent.name,
          ui_messages: run.ui_messages,
          invocation: null,
          turns: run.turns.map((t) => ({
            ...t,
            notices: t.notices ?? [],
            agent_version: this.agentVersions.get(t.agent_version_id)![1],
          })),
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
              created_by: this.me,
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
    if (path === "/api/triggers" && method === "GET") {
      const kind = url.searchParams.get("target_kind");
      const target = Number(url.searchParams.get("target_id"));
      return reply(
        200,
        this.schedules.filter((t) => kind === null || (t.target_kind === kind && t.target_id === target)),
      );
    }
    if (path === "/api/triggers" && method === "POST") {
      const refused = this.refuseUnlessBuilder();
      if (refused) return fail(403, refused);
      if (body.kind === "event") {
        const known = FILTER_FIELDS[body.event_type as string] ?? [];
        const unknown = Object.keys(body.filter ?? {}).find((k) => !known.includes(k));
        if (unknown) return fail(422, `Unknown filter field '${unknown}' (known: ${known.sort().join(", ") || "none"}).`);
        if (body.event_type === "chat_message" && !body.filter?.chat_id)
          return fail(422, "Filter field 'chat_id' is required.");
      }
      const archived = this.archivedDestination(body.deliver_to);
      if (archived) return fail(409, archived);
      const trigger = this.triggerOut(body);
      this.schedules.push(trigger);
      this.addListener(trigger);
      // A webhook trigger's secret is in the answer that made it, and only there.
      const secret = trigger.webhook_url ? { webhook_secret: `whsec_${trigger.id}_1` } : {};
      return reply(201, { ...trigger, ...secret });
    }
    if ((m = path.match(/^\/api\/triggers\/(\d+)\/webhook-secret$/)) && method === "POST") {
      const at = this.schedules.findIndex((t) => t.id === Number(m![1]));
      if (at < 0) return fail(404, "Not Found");
      const refused = this.refuseUnlessBuilder();
      if (refused) return fail(403, refused);
      const old = this.schedules[at];
      if (!old.webhook_url) return fail(409, "This trigger has no webhook.");
      const rotations = (this.webhookRotations.get(old.id as number) ?? 1) + 1;
      this.webhookRotations.set(old.id as number, rotations);
      this.schedules[at] = { ...old, webhook_url: `${WEBHOOK_URL}token-${old.id}-${rotations}` };
      return reply(200, { ...this.schedules[at], webhook_secret: `whsec_${old.id}_${rotations}` });
    }
    if ((m = path.match(/^\/api\/triggers\/(\d+)$/)) && method !== "GET") {
      const at = this.schedules.findIndex((t) => t.id === Number(m![1]));
      if (at < 0) return fail(404, "Not Found");
      const refused = this.refuseUnlessBuilder();
      if (refused) return fail(403, refused);
      if (method === "DELETE") {
        this.schedules.splice(at, 1);
        return reply(204, null);
      }
      const old = this.schedules[at];
      const chosen = (old.deliver_to as { id: number } | null)?.id;
      const archived = body.deliver_to !== chosen && this.archivedDestination(body.deliver_to);
      if (archived) return fail(409, archived);
      this.schedules[at] = {
        ...old,
        ...this.triggerOut(body, old.id as number),
        // A webhook trigger keeps its URL as it's edited.
        ...(old.webhook_url && body.event_type === "webhook_received" ? { webhook_url: old.webhook_url } : {}),
        last_fired_at: old.last_fired_at,
        last_status: old.last_status,
        last_error: old.last_error,
      };
      this.addListener(this.schedules[at]);
      return reply(200, this.schedules[at]);
    }
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
        this.live(url.searchParams.get("archived") === "true").map((w) => ({
          id: w.id,
          name: w.name,
          description: w.description,
          version: w.versions.length,
          version_id: w.versions.at(-1),
          node_count: this.current(w).nodes.length,
          updated_at: w.updated_at,
          created_by: w.created_by,
          updated_by: w.updated_by,
          archived_at: w.archived_at,
          archived_by: w.archived_by,
          contains: [...this.contains(this.current(w))],
        })),
      );
    }
    if (path === "/api/workflows" && method === "POST") {
      // The server checks the graph first; the name only clashes when the save commits.
      const error = this.check(body);
      if (error) return fail(422, error);
      if ([...this.workflows.values()].some((w) => w.name === body.name)) {
        return fail(409, "The organisation already has a workflow with that name");
      }
      const workflow: WorkflowRow = {
        id: this.id(),
        name: body.name,
        description: body.description ?? "",
        layout: {},
        versions: [],
        archived_at: null,
        archived_by: null,
        created_at: NOW,
        updated_at: NOW,
        created_by: this.me,
        updated_by: this.me,
      };
      this.workflows.set(workflow.id, workflow);
      this.applyLayout(workflow, body);
      this.saveVersion(workflow, body.nodes, body.edges, body.note ?? null, body);
      return reply(201, this.workflowOut(workflow));
    }
    if ((m = path.match(/^\/api\/workflows\/(\d+)(\/.*)?$/))) {
      const workflow = this.workflows.get(Number(m[1]));
      const rest = m[2] ?? "";
      if (!workflow) return fail(404, "Not Found");
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
        if ([...this.workflows.values()].some((w) => w.name === body.name && w !== workflow)) {
          return fail(409, "The organisation already has a workflow with that name");
        }
        const refused = this.applyGuards(workflow, body);
        if (refused) return fail(403, refused);
        workflow.name = body.name;
        workflow.description = body.description ?? "";
        workflow.updated_by = this.me;
        this.applyLayout(workflow, body);
        this.saveVersion(workflow, body.nodes, body.edges, body.note ?? null, body);
        return reply(200, this.workflowOut(workflow));
      }
      if ((rest === "/archive" || rest === "/unarchive") && method === "POST") {
        if (rest === "/unarchive") {
          workflow.archived_at = null;
          workflow.archived_by = null;
        } else if (workflow.archived_at === null) {
          workflow.archived_at = new Date().toISOString();
          workflow.archived_by = this.me;
          for (const t of this.schedules) {
            if (t.target_kind === "workflow" && t.target_id === workflow.id) t.enabled = false;
          }
        }
        return reply(200, this.workflowOut(workflow));
      }
      if (rest === "/used-by" && method === "GET") {
        const nests = this.live().filter((w) => this.contains(this.current(w)).has(workflow.id));
        return reply(200, {
          workflows: nests.map((w) => ({
            id: w.id,
            name: w.name,
            version: w.versions.length,
            direct: this.current(w).nodes.some(
              (n) => n.kind === "workflow" && this.workflowVersions.get(n.version_id!)!.workflow_id === workflow.id,
            ),
            triggers: [],
          })),
          triggers: this.schedules
            .filter((t) => t.target_kind === "workflow" && t.target_id === workflow.id)
            .map(({ id, enabled, cron, description, timezone, next_run_at }) => ({
              id,
              enabled,
              cron,
              description,
              timezone,
              next_run_at,
            })),
          chats: [...this.chats.values()]
            .filter((c) => c.members.some((x) => x.kind === "workflow" && x.target_id === workflow.id))
            .map((c) => ({ id: c.id, name: c.name })),
        });
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
        const schema = this.current(workflow).input_schema;
        if (schema) {
          // The server checks it against the schema; here, only that required fields are there.
          const value = body?.input_json ?? body?.input;
          const required = (schema as { required?: string[] }).required ?? [];
          const missing = required.filter(
            (f) => typeof value !== "object" || value === null || !(f in value),
          );
          if (missing.length) {
            const errors = missing.map((f) => ({ path: "/", message: `'${f}' is a required property` }));
            const message = `The input doesn't fit the workflow's input schema: ${errors.map((e) => `at ${e.path}: ${e.message}`).join("; ")}`;
            return reply(422, { detail: { message, errors } });
          }
          if (this.current(workflow).nodes.length === 0) {
            return fail(422, "This workflow has no nodes to run");
          }
          const run = this.addRun(workflow, {
            status: "queued",
            input: JSON.stringify(value),
            input_json: value,
            started_at: null,
          });
          return reply(201, this.runSummary(run));
        }
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
          ...this.runOut(run),
          workflow_name: workflow.name,
        });
      }
      if ((m = rest.match(/^\/runs\/(\d+)\/cancel$/)) && method === "POST") {
        const run = this.runs.get(Number(m[1]));
        if (!run || run.workflow_id !== workflow.id) return fail(404, "Not Found");
        if (!["queued", "running", "waiting"].includes(run.status)) {
          return fail(409, "This run has already ended.");
        }
        Object.assign(run, {
          status: "failed",
          error: `Cancelled by ${this.me.first_name} ${this.me.last_name}.`,
          finished_at: NOW,
          waiting_since: null,
        });
        for (const step of run.steps) {
          if (step.status !== "running" && step.status !== "waiting") continue;
          for (const id of step.waiting_on_request_ids) {
            const approval = this.schemaChanges.find((r) => r.id === id);
            if (approval?.status === "pending") {
              Object.assign(approval, { status: "cancelled", decided_at: NOW, resumed_at: NOW });
              this.clearApprovalNotifications(id);
            }
          }
          Object.assign(step, {
            status: "failed",
            error: "The run stopped before this step finished.",
            finished_at: NOW,
            waiting_on_request_ids: [],
          });
        }
        return reply(200, { ...this.runOut(run), workflow_name: workflow.name });
      }
      if ((m = rest.match(/^\/runs\/(\d+)\/subworkflow$/)) && method === "GET") {
        const run = this.runs.get(Number(m[1]));
        if (!run || run.workflow_id !== workflow.id) return fail(404, "Not Found");
        // The version pinned at the path's workflow node, along the run's graph.
        let version = this.workflowVersions.get(run.workflow_version_id)!;
        for (const key of url.searchParams.getAll("path")) {
          const node = version.nodes.find((n) => n.key === key);
          if (!node || node.kind !== "workflow") return fail(404, "Not Found");
          version = this.workflowVersions.get(node.version_id!)!;
        }
        return reply(200, this.versionOut(version));
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
              created_by: this.me,
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
        workflow.updated_by = this.me;
        this.saveVersion(workflow, old.nodes, old.edges, `Restored from v${old.version}`, old);
        return reply(200, this.workflowOut(workflow));
      }
    }
    if (path === "/api/chats" && method === "GET") {
      const archived = url.searchParams.get("archived") === "true";
      // Most recently active first; everything here happens at NOW, so the newest first.
      return reply(
        200,
        this.organisationChats()
          // Every chat, joined or not, that isn't archived (or, asked for, that is).
          .filter((c) => Boolean(c.archived_at) === archived)
          .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id)
          .map((c) => ({ ...this.chatSummary(c), unread_count: this.unreadCount(c) })),
      );
    }
    if (path === "/api/chats" && method === "POST") {
      if (body.agent_id != null) {
        // **Chat with this agent**: that agent only, named for it unless a name is given.
        const agent = this.agents.get(Number(body.agent_id));
        if (!agent) return fail(404, "Unknown agent");
        const refused = this.invokeRefusal("agent", agent);
        if (refused) return fail(403, refused);
        if (agent.archived_at) return fail(409, `The agent ${agent.name} is archived. Unarchive it first.`);
        const base = `${agent.name} – ${fullName(this.me)} – ${NOW.slice(0, 10)}`;
        const taken = new Set(this.organisationChats().map((c) => c.name));
        let n = 1;
        while (taken.has(n === 1 ? base : `${base} (${n})`)) n += 1;
        const name = body.name ?? (n === 1 ? base : `${base} (${n})`);
        if (taken.has(name)) return fail(409, CHAT_NAME_TAKEN);
        const chat = this.addChat(name, { description: body.description ?? "" });
        this.addChatTarget(chat, "agent", agent);
        return reply(201, this.chatOut(chat));
      }
      if (this.organisationChats().some((c) => c.name === body.name)) {
        return fail(409, CHAT_NAME_TAKEN);
      }
      const chat = this.addChat(body.name, { description: body.description ?? "" });
      // A new chat's first agent is the organisation's documents assistant.
      this.addChatTarget(chat, "agent", this.documentsAssistant());
      return reply(201, this.chatOut(chat));
    }
    if ((m = path.match(/^\/api\/chats\/(\d+)(\/.*)?$/))) {
      const chat = this.chats.get(Number(m[1]));
      const rest = m[2] ?? "";
      // Another organisation's chat doesn't exist here.
      if (!chat || chat.organisation_id !== this.organisation.id) return fail(404, "Not Found");
      if (rest === "" && method === "GET") return reply(200, this.chatOut(chat));
      if (rest === "/stream" && method === "GET") {
        this.streamReads.push(url.search);
        const refused = this.streamRefusal(route);
        if (refused) return refused;
        return this.events(route, this.chatEvents(chat, url.searchParams.get("last_event_id")));
      }
      if (rest === "/messages" && method === "GET") {
        this.messageReads.push(url.search);
        this.advanceInvocations(chat);
        return reply(200, this.messagesPage(chat, url.searchParams));
      }
      if (rest === "/mentionable" && method === "GET") return reply(200, this.mentionable(chat));
      if (rest === "/archive" || rest === "/unarchive") {
        if (method !== "POST") return fail(405, "Method Not Allowed");
        const refused = this.roleOf(this.me.id) !== "admin";
        if (refused) return fail(403, `${this.roleOf(this.me.id) === "editor" ? "Editors" : "Participants"} can't archive or unarchive chats. Ask an admin.`);
        if (rest === "/archive" && !chat.archived_at) Object.assign(chat, { archived_at: NOW, archived_by: this.me });
        if (rest === "/unarchive") Object.assign(chat, { archived_at: null, archived_by: null });
        return reply(200, this.chatOut(chat));
      }
      // An archived chat is read-only: checked before anything else (Jev's check included).
      const writes = ["/join", "/messages", "/members", ""].includes(rest.replace(/\/\d+$/, ""));
      if (chat.archived_at && writes && (method === "POST" || method === "PUT" || method === "DELETE")) {
        return fail(409, ARCHIVED);
      }
      if (rest === "/join" && method === "POST") {
        if (this.inChat(chat)) return fail(409, "Already a member of this chat.");
        // What was there to read before they joined counts as seen.
        this.addChatMember(chat, this.me).last_read = chat.messages.at(-1)?.id ?? null;
        return reply(200, this.chatOut(chat));
      }
      if (rest === "/leave" && method === "POST") {
        const mine = this.myMembership(chat);
        if (!mine) return fail(409, "Not a member of this chat.");
        // Anyone, the last person too: the chat stays.
        chat.members = chat.members.filter((x) => x !== mine);
        return reply(204);
      }
      if (rest === "/read" && method === "POST") {
        const mine = this.myMembership(chat);
        if (!mine) return fail(403, "Only the chat's members keep track of what they've read in it.");
        this.chatReads.push([chat.id, body.message_id]);
        const seen = Math.min(body.message_id, chat.messages.at(-1)?.id ?? 0);
        mine.last_read = Math.max(mine.last_read ?? 0, seen);
        // And the mention, reply and run failed notifications about what they've read.
        for (const n of this.notifications) {
          const about = n.organisation.id === this.organisation.id && n.ref.chat_id === chat.id;
          if (n.mine && about && ["mention", "reply", "run_failed"].includes(n.kind) && Number(n.ref.message_id) <= seen) {
            n.read_at ??= NOW;
          }
        }
        return reply(204);
      }
      if (rest === "/messages" && method === "POST") {
        const members = Object.values(this.mentionable(chat, true)).flat();
        const mentions: ChatMention[] = [];
        const missing: ChatMention[] = [];
        for (const { kind, id } of (body.mentions ?? []) as ChatMention[]) {
          const member = members.find((x) => x.kind === kind && x.id === id);
          if (!member) {
            if (kind === "user") return fail(422, "Only the chat's members can be mentioned in it.");
            const target = this.chatTarget(kind, id);
            if (!target) return fail(422, `Unknown ${kind}`);
            if (!missing.some((x) => x.kind === kind && x.id === id)) missing.push({ kind, id, name: target.name });
            continue;
          }
          const twice = mentions.some((x) => x.kind === kind && x.id === id);
          if (!twice) mentions.push({ kind, id, name: member.name });
        }
        if (missing.length) {
          const names = missing.map((x) => `${x.name} (${x.kind})`).join(" or ");
          return reply(422, {
            detail: { message: `This chat doesn't include ${names}. Add it to this chat to mention it.`, missing },
          });
        }
        // The mention guards, in the API's order: the count, each mention's invocable-by, the rate.
        const targets = mentions.filter((x) => x.kind !== "user");
        if (targets.length > this.mentionsPerMessage) {
          return fail(
            422,
            `A message can mention at most ${this.mentionsPerMessage} agents and workflows. Mention fewer, or split it into several messages.`,
          );
        }
        const refusals = new Map<string, string>();
        for (const x of targets) {
          const refused = this.invokeRefusal(x.kind, (x.kind === "agent" ? this.agents : this.workflows).get(x.id)!);
          if (refused) refusals.set(`${x.kind}:${x.id}`, refused);
        }
        const allowed = targets.length - refusals.size;
        if (allowed && this.mentionsMade + allowed > this.mentionRate) {
          return route.fulfill({
            status: 429,
            contentType: "application/json",
            headers: { "Retry-After": String(this.mentionRetryAfter) },
            body: JSON.stringify({
              detail: `You've mentioned agents and workflows too often. Try again in ${this.mentionRetryAfter} seconds; your message hasn't been posted.`,
            }),
          });
        }
        // Jev checks the message before anything is written.
        if (this.jevUp === false) return fail(503, "Couldn't check this message, try again.");
        if (this.jevFlags.test(body.body)) {
          return reply(422, {
            detail: {
              message: "Jev flagged this message as a likely prompt injection, so it wasn't posted.",
              reason: "Jev judged this a likely prompt injection (probability 0.97).",
            },
          });
        }
        this.mentionsMade += allowed;
        // Posting joins.
        if (!this.inChat(chat)) this.addChatMember(chat, this.me).last_read = chat.messages.at(-1)?.id ?? null;
        const message = this.addChatMessage(chat, body.body, this.me, mentions);
        const invoked = targets.map((x) =>
          this.invoke(
            chat,
            message,
            x.kind as Kind,
            (x.kind === "agent" ? this.agents : this.workflows).get(x.id)!,
            refusals.get(`${x.kind}:${x.id}`),
          ),
        );
        const ids = new Set(invoked.map((i) => i.id));
        return reply(
          201,
          this.messagesOut(
            chat,
            chat.messages.filter((x) => x === message || ids.has(x.invocation_id!)),
          ),
        );
      }
      if (!this.inChat(chat)) return fail(403, "Only the chat's members can do that: join it first.");
      if (rest === "" && method === "PUT") {
        if (this.organisationChats().some((c) => c.name === body.name && c !== chat)) {
          return fail(409, CHAT_NAME_TAKEN);
        }
        Object.assign(chat, {
          updated_by: this.me,
          name: body.name,
          description: body.description ?? "",
        });
        return reply(200, this.chatOut(chat));
      }
      if (rest === "/members" && method === "POST") {
        const kind = body.kind as ChatMention["kind"];
        if (kind === "user") return fail(403, "Nobody adds a person to a chat: people join it themselves.");
        if (!this.chatTarget(kind, body.id)) return fail(422, `Unknown ${kind}`);
        if (chat.members.some((x) => x.kind === kind && x.target_id === body.id)) {
          return fail(409, "Already a member of this chat.");
        }
        this.addChatTarget(chat, kind, { id: body.id });
        return reply(201, this.chatOut(chat));
      }
      if ((m = rest.match(/^\/members\/(\d+)$/)) && method === "DELETE") {
        const member = chat.members.find((x) => x.id === Number(m![1]));
        if (!member) return fail(404, "Not Found");
        if (member.kind === "user") return fail(403, "Nobody removes a person from a chat: people leave it themselves.");
        chat.members = chat.members.filter((x) => x !== member);
        return reply(204);
      }
    }
    return fail(404, `fake API: no route for ${method} ${path}`);
  }

  private runSummary({ steps: _steps, stale_pins: _pins, ...run }: RunRow) {
    const invocation = [...this.invocations.values()].find(
      (i) => i.target_kind === "workflow" && i.run_id === run.id,
    );
    const chat = invocation && this.chats.get(invocation.chat_id);
    return {
      ...run,
      workflow_version: this.workflowVersions.get(run.workflow_version_id)!.version,
      chat_id: chat?.id ?? null,
      chat_name: chat?.name ?? null,
    };
  }

  /** A run as its page shows it. */
  private runOut(run: RunRow) {
    return {
      ...this.runSummary(run),
      version: this.versionOut(this.workflowVersions.get(run.workflow_version_id)!),
      steps: run.steps,
      stale_pins: run.stale_pins,
      started_by_agent: run.started_by_agent ?? null,
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
      if (n.kind === "approval") {
        if (!(n.prompt_template ?? "").trim()) {
          return `nodes.${n.key}.prompt_template: Write the prompt its approvers see`;
        }
        continue;
      }
      if (n.kind === "agent" && !this.agentVersions.has(n.version_id!)) {
        return "Unknown agent version";
      }
      if (n.kind === "workflow") {
        const v = this.workflowVersions.get(n.version_id!);
        if (!v) return "Unknown workflow version";
        if (workflow && v.workflow_id === workflow.id) return "A workflow can't contain itself";
        if (workflow && this.contains(v).has(workflow.id)) {
          return "A workflow can't contain itself, even through another workflow";
        }
      }
    }
    // Conditions use only the allowed operators (the server checks fields and size too).
    const operators = (c: Json | null | undefined): string[] =>
      Array.isArray(c)
        ? c.flatMap(operators)
        : c !== null && typeof c === "object"
          ? Object.entries(c).flatMap(([op, args]) => [op, ...operators(args)])
          : [];
    for (const e of body.edges) {
      const bad = operators(e.condition).find((op) => !CONDITION_OPERATORS.has(op));
      if (bad) return `edges.${e.source}→${e.target}.condition: The operator '${bad}' is not allowed`;
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
