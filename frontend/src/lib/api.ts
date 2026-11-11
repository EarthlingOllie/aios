import type { UIMessage } from "ai";

/** A user's account: who's signed in (with their organisation, `Me`), and the dev picker's users. */
export type Account = {
  id: number;
  first_name: string;
  last_name: string;
  email: string;
};

/** A user as everything else names them (who made, changed or started something; a member). */
export type User = {
  id: number;
  /** Their full name, "First Last". */
  name: string;
};

/** The name the app shows a user by, as the API does: "First Last". */
export function fullName(user: Pick<Account, "first_name" | "last_name">): string {
  return `${user.first_name} ${user.last_name}`;
}

/** What the sign-in page offers besides email and password (`GET /api/auth/options`). */
export type AuthOptions = {
  /** The development "sign in as" picker (`DEV_LOGIN_ENABLED`): `devUsers()` and `devSignIn()`. */
  dev_users: boolean;
};

export type Registration = {
  first_name: string;
  last_name: string;
  email: string;
  password: string;
  password_confirm: string;
  /** The token of the invitation link whose page they came from: they join its organisation. */
  invite?: string;
};

/**
 * Who made something of the organisation's (an agent, skill, workflow or schedule) and who last
 * changed it. Null once that member's account is deleted, or for what was made before
 * organisations, where it isn't known.
 */
export type Provenance = { created_by: User | null; updated_by: User | null };

/** The organisation the signed-in user is in, as `/api/me` says it: what the shell shows. */
export type OrganisationSummary = {
  id: number;
  name: string;
  /** Made for one user on sign-up (or on leaving another organisation). */
  personal: boolean;
  /** Whether the signed-in user created it; the creator alone can rename it. */
  is_creator: boolean;
};

/** The signed-in user (`GET /api/me`), with their organisation. */
export type Me = Account & { organisation: OrganisationSummary };

export type OrganisationMember = {
  user: User;
  joined_at: string;
  /** The invitation link they came in through, if any. */
  invite_id: string | null;
};

/** The Organisation page. */
export type Organisation = OrganisationSummary & {
  /** Null once the creator's account is deleted. */
  creator: User | null;
  /** In the order they joined. */
  members: OrganisationMember[];
  created_at: string;
};

/** An open invitation link into the organisation, as its creator lists them. */
export type Invite = {
  id: string;
  /** Null once their account is deleted. */
  creator: User | null;
  created_at: string;
  expires_at: string;
};

/** A link just made: the only time its token (and so the link) is shown. */
export type NewInvite = Invite & { token: string; url: string };

/** What the invite page shows of an open link, to anyone holding it. */
export type Invitation = { organisation: { id: number; name: string }; expires_at: string };

/** What can be in an organisation, as a refused accept counts it. */
export type ContentKind = "agent" | "workflow" | "skill" | "chat" | "schedule" | "document" | "table";

/** Accepting refused (a 409) because the user's organisation has things in it, which accepting
 * would delete with it: the reason, and how many of each kind there are. */
export type NotEmpty = { message: string; contents: { kind: ContentKind; count: number }[] };

/** The organisation's data store: documents and search wait until it's "ready". */
export type DataStoreStatus = "provisioning" | "ready" | "failed" | "deleting" | "deleted";

/** What a data store uses, as last measured (null until it's first measured), and its caps. */
export type DataStoreUsage = {
  db_bytes: number | null;
  db_quota_bytes: number;
  files_bytes: number | null;
  files_quota_bytes: number;
  measured_at: string | null;
};

/** The organisation's settings (the Organisation page), kept on its data store. */
export type Settings = {
  /** Copy agent and workflow runs into the data store's `history` tables. */
  export_run_history: boolean;
  /** The data store has its `history` tables (made once the export is first switched on). */
  history_ready: boolean;
  /** When the export last copied runs into the data store. */
  history_exported_at: string | null;
  /** Embed the exported messages, so agents can search past runs by meaning (only while the
   * export is on). */
  embed_run_history: boolean;
  /** When embedding last caught up (embedded every exported message waiting). */
  history_embedded_at: string | null;
  /** Why embedding last failed, if it hasn't caught up since (it's tried again later). */
  history_embed_error: string | null;
};

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export type DocumentInfo = {
  id: number;
  filename: string;
  status: DocumentStatus;
  error: string | null;
  chunk_count: number;
  size_bytes: number;
  /** The member who uploaded it; null for documents from before it was recorded. */
  uploaded_by: User | null;
  created_at: string;
};

/** A column of a table in the store's `data` schema. */
export type DataColumn = {
  name: string;
  /** As Postgres names it, e.g. `numeric` or `vector(1024)`. */
  type: string;
  nullable: boolean;
  description: string | null;
  /** An embedding: its values come as their type and dimension, never their numbers. */
  embedding: boolean;
};

/** A table in the store's `data` schema: what the organisation's agents keep. */
export type DataTable = {
  name: string;
  description: string | null;
  columns: DataColumn[];
  /** Null if it couldn't be told. */
  row_count: number | null;
  /** The count is the database's estimate (a big table), not exact. */
  row_count_estimated: boolean;
};

/** A page of a table's rows: each value as text (null for NULL), cut to a preview. */
export type DataRowsPage = {
  columns: string[];
  rows: {
    /** Names the row when asking for one of its values in full. */
    key: string;
    values: (string | null)[];
    /** The values cut short, by their place in `values`. */
    truncated: number[];
  }[];
  offset: number;
  has_more: boolean;
};

export type DataRowsQuery = {
  offset: number;
  limit: number;
  sort?: string;
  order?: "asc" | "desc";
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

export type SkillSummary = Provenance & {
  id: number;
  name: string;
  description: string;
  version: number;
  version_id: number;
  updated_at: string;
  /** How many live agents' current versions use some version of it. */
  agent_count: number;
};

export type SkillVersionSummary = {
  id: number;
  version: number;
  note: string | null;
  created_at: string;
  /** The member who saved it. */
  created_by: User | null;
};

export type SkillVersion = SkillVersionSummary & {
  /** The skill's name when this version was saved. */
  name: string;
  description: string;
  content: string;
};

export type Skill = Provenance & {
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
  /** The member who saved it. */
  created_by: User | null;
  skill_count: number;
  tool_count: number;
  run_count: number;
};

export type AgentVersion = {
  id: number;
  version: number;
  note: string | null;
  created_at: string;
  /** The member who saved it. */
  created_by: User | null;
  base_prompt: string;
  model: string | null;
  /** The exact instructions this version sends to the model. */
  instructions: string;
  skills: AgentSkill[];
  tools: AgentTool[];
};

/** When an agent was archived and by whom: both null while it isn't. */
export type Archived = {
  archived_at: string | null;
  archived_by: User | null;
};

export type AgentSummary = Provenance & Archived & {
  id: number;
  name: string;
  description: string;
  version: number;
  /** The current version's id, which a workflow node pins to run the agent. */
  version_id: number;
  skill_count: number;
  tool_count: number;
  /** The skills (any version of each) and tools its current version has, in order. */
  skill_ids: number[];
  tool_keys: string[];
  run_count: number;
  updated_at: string;
};

export type Agent = Provenance & Archived & {
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

export type AgentRunStatus = "running" | "succeeded" | "failed";

export type RunSummary = {
  id: number;
  agent_id: number;
  agent_version_id: number;
  agent_version: number;
  title: string | null;
  message_count: number;
  /** The trigger (e.g. a schedule) that started the run, if one did and it still exists. */
  trigger_id: number | null;
  /** How its latest turn is going or went, and when it began and ended. Null, with the
   * counters, for a run from before these were kept (see `lib/agent-runs`). */
  status: AgentRunStatus | null;
  started_at: string | null;
  finished_at: string | null;
  /** Over all its turns: the model's tool calls, and the tokens of each model call. */
  tool_call_count: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  /** The chat a mention in it started it from, while that chat exists. */
  chat_id: number | null;
  chat_name: string | null;
  /** The member who started it (by hand, by a mention, or by starting the workflow run it's a
   * node's conversation in); null for a schedule's run, or once that user is deleted. */
  created_by: User | null;
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

export type SchemaChangeStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "expired"
  | "applied"
  | "failed"
  | "stale"
  | "cancelled";

/** An agent's request to change the tables in the organisation's data store. */
export type SchemaChangeRequest = {
  id: string;
  data_store_id: string;
  status: SchemaChangeStatus;
  summary: string;
  reason: string;
  /** Exactly what runs on approval. */
  sql: string;
  ops: Record<string, unknown>[];
  /** The agent run that asked, if it still exists. */
  run: { agent_id: number; run_id: number; title: string | null } | null;
  /** The tool call that asked, so a chat shows the request at that call. */
  tool_call_id: string | null;
  note: string | null;
  error: string | null;
  created_at: string;
  decided_at: string | null;
  applied_at: string | null;
  /** When the run that asked was given the outcome. */
  resumed_at: string | null;
  /** Whether the signed-in user may approve or reject it (it's still pending). */
  can_decide: boolean;
  /** What approving it takes typed, if it drops anything: the tables' names. */
  confirm_with: string | null;
};

/** What one of a request's drops would remove, as the store is now. */
export type Removal = {
  table: string;
  /** The column it drops; null for the whole table. */
  column: string | null;
  /** The table's rows; null if it isn't there any more. */
  rows: number | null;
  /** A column's: how many rows have a value in it. */
  values: number | null;
  /** A table's: its columns. */
  columns: string[] | null;
  /** Whether `rows` is Postgres's estimate (or null: none), as counting took too long. */
  estimated: boolean;
};

/** A table or column a drop took away, until it's restored or purged. */
export type DroppedObject = {
  id: number;
  kind: "table" | "column";
  /** A table's name, or the table a column is in. */
  table: string;
  column: string | null;
  dropped_name: string;
  dropped_at: string;
  purge_at: string;
  /** Why it can't be restored now, if it can't (its name has been taken since, say). */
  blocked: string | null;
  /** Why the last restore asked for was refused, if it was. */
  restore_error: string | null;
};

/** Something in the app for the signed-in user to see. */
export type Notification = {
  id: string;
  /** What it's about: `schema_change_request` (a request waiting for a decision) for now. */
  kind: string;
  /** For a schema change request, `request_id` and `data_store_id`. */
  ref: Record<string, string>;
  /** What it says, in a line; null when what it's about has gone. */
  title: string | null;
  created_at: string;
  read_at: string | null;
};

export type NotificationList = {
  /** How many of the user's are unread, all told. */
  unread: number;
  /** Unread first, then read; newest first within each. */
  notifications: Notification[];
};

export type Run = RunSummary & {
  /** The agent's name, so the page needn't read the agent as well. */
  agent_name: string;
  ui_messages: UIMessage[];
  messages: ModelMessage[];
  /** The pinned version's instructions: the system prompt of every model call. */
  instructions: string;
  /** The schema change requests the run made, oldest first. */
  schema_change_requests: SchemaChangeRequest[];
  /** Whether it waits on one of them: it takes no new messages until it's resumed. */
  waiting: boolean;
};

export type WorkflowNodeKind = "agent" | "workflow";

/** Where the editor draws a node: its top-left corner. */
export type NodePosition = { x: number; y: number };

export type WorkflowNode = {
  key: string;
  kind: WorkflowNodeKind;
  /** The agent version or workflow version the node runs. */
  version_id: number;
  /** The agent or workflow that version belongs to. */
  target_id: number;
  name: string;
  version: number;
  /** The agent's or workflow's current version; null if it has since been deleted. */
  latest_version: number | null;
  latest_version_id: number | null;
};

export type WorkflowEdge = { source: string; target: string };

export type WorkflowVersionSummary = {
  id: number;
  version: number;
  note: string | null;
  created_at: string;
  /** The member who saved it. */
  created_by: User | null;
  node_count: number;
};

export type WorkflowVersion = {
  id: number;
  version: number;
  note: string | null;
  created_at: string;
  /** The member who saved it. */
  created_by: User | null;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
};

export type WorkflowSummary = Provenance & {
  id: number;
  name: string;
  description: string;
  version: number;
  version_id: number;
  node_count: number;
  updated_at: string;
  /** The workflows its current version runs, at any depth; none of them can run this one. */
  contains: number[];
};

export type Workflow = Provenance & {
  id: number;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  current_version: WorkflowVersion;
  /** Saved positions by node key; nodes without one are laid out automatically. */
  layout: Record<string, NodePosition>;
};

export type WorkflowInput = {
  name: string;
  description: string;
  nodes: { key: string; kind: WorkflowNodeKind; version_id: number }[];
  edges: WorkflowEdge[];
  /** Omitted: keep the saved layout. */
  layout?: Record<string, NodePosition>;
  note?: string;
  /** The version the edit started from; a 409 if the workflow has moved on. Omitted: no check. */
  base_version?: number;
};

/** waiting: paused until the user decides a schema change request one of its nodes made. */
export type WorkflowRunStatus = "queued" | "running" | "waiting" | "succeeded" | "failed";

export type WorkflowRunSummary = {
  id: number;
  workflow_id: number;
  workflow_version_id: number;
  workflow_version: number;
  status: WorkflowRunStatus;
  input: string;
  output: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** While it waits, since when; and how long it waited before (seconds). Waiting doesn't
   * count towards its time limit. */
  waiting_since: string | null;
  waited_seconds: number;
  /** The chat a mention in it started it from, while that chat exists. */
  chat_id: number | null;
  chat_name: string | null;
  /** The member who started it (by hand, or by a mention); null for a schedule's run, or once
   * that user is deleted. */
  created_by: User | null;
  /** The trigger (a schedule) that started it, if one did and it still exists. */
  trigger_id: number | null;
};

/** A node that has started in a run; see plan/workflow-editor/runs-api.md. */
export type WorkflowRunStep = {
  id: number;
  /** Node keys from the run's workflow down to this node. */
  path: string[];
  kind: WorkflowNodeKind;
  /** waiting: an agent node waiting for a schema change request, or a workflow node one of
   * whose nodes waits. */
  status: "running" | "waiting" | "succeeded" | "failed";
  input: string;
  output: string | null;
  error: string | null;
  /** Agent nodes: the conversation, at /agents/{agent_id}/runs/{agent_run_id}. */
  agent_id: number | null;
  agent_run_id: number | null;
  /** While an agent node waits: the schema change requests it waits on, oldest first. */
  waiting_on_request_ids: string[];
  started_at: string;
  finished_at: string | null;
};

/** A version a waiting run pins that's no longer current: the run resumes on it regardless. */
export type StalePin = {
  /** The node pinning it, from the run's workflow down; [] for the run's own workflow. */
  path: string[];
  kind: WorkflowNodeKind;
  name: string;
  pinned_version: number;
  /** null once the agent or workflow has been deleted. */
  current_version: number | null;
};

export type WorkflowRun = WorkflowRunSummary & {
  /** The pinned version's graph. */
  version: WorkflowVersion;
  /** In the order they started. */
  steps: WorkflowRunStep[];
  /** While it waits: the pins it would resume on that are no longer current. */
  stale_pins: StalePin[];
};

/** A workflow run as its page reads it. */
export type WorkflowRunPage = WorkflowRun & {
  /** The workflow's name, so the page needn't read the workflow as well. */
  workflow_name: string;
};

export type TriggerTargetKind = "agent" | "workflow";
export type TriggerStatus = "running" | "succeeded" | "failed";

/** Starts an agent or a workflow by itself. Schedules (cron) are the only kind so far. */
export type Trigger = Provenance & {
  id: number;
  kind: "schedule";
  target_kind: TriggerTargetKind;
  target_id: number;
  target_name: string;
  enabled: boolean;
  /** The message an agent's run starts with, or a workflow run's input. */
  input: string;
  cron: string;
  /** The cron expression in plain English. */
  description: string;
  timezone: string;
  /** Null while disabled. */
  next_run_at: string | null;
  last_fired_at: string | null;
  /** For a workflow, how the run it last started is going (a worker runs it). */
  last_status: TriggerStatus | null;
  last_error: string | null;
  /** The agent run it last started (agents). */
  last_run_id: number | null;
  /** The workflow run it last started (workflows). */
  last_workflow_run_id: number | null;
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

/** A schedule that would start runs of an agent, or of a workflow that runs it. */
export type DependentTrigger = {
  id: number;
  enabled: boolean;
  cron: string;
  description: string;
  timezone: string;
  next_run_at: string | null;
};

/** A schema change request a run that depends on an agent waits on. */
export type DependentRequest = { id: string; summary: string; status: SchemaChangeStatus };

/** What depends on an agent: what deleting it would cut off (GET /api/agents/{id}/dependents). */
export type AgentDependents = {
  /** Its chat runs still going: waiting on a schema change request, or with a turn underway. */
  agent_runs: {
    id: number;
    title: string | null;
    waiting: boolean;
    running: boolean;
    trigger_id: number | null;
    updated_at: string;
    requests: DependentRequest[];
  }[];
  /** Workflow runs still going (queued, running or waiting) that run it, at any depth. */
  workflow_runs: {
    id: number;
    workflow_id: number;
    workflow_name: string;
    status: WorkflowRunStatus;
    created_at: string;
    /** Its agent nodes waiting on a request, by path ("review/critic"). */
    waiting_nodes: string[];
    requests: DependentRequest[];
  }[];
  /** Live workflows whose current version runs it, directly or through a nested workflow. */
  workflows: {
    id: number;
    name: string;
    version: number;
    direct: boolean;
    triggers: DependentTrigger[];
  }[];
  /** Its own schedules, deleted with it. */
  triggers: DependentTrigger[];
};

/** What uses an agent, as its archive confirmation shows it (GET /api/agents/{id}/used-by). */
export type AgentUsedBy = {
  /** Live workflows that run it at any depth, with their schedules. */
  workflows: AgentDependents["workflows"];
  /** Its own schedules, which archiving pauses. */
  triggers: DependentTrigger[];
  /** The chats it's a member of. */
  chats: { id: number; name: string }[];
  /** The data tables it will leave read-only: none until tables have owners. */
  tables: { name: string }[];
};

/** How to resolve what depends on an agent as it's deleted (DELETE /api/agents/{id}). */
export type AgentDeleteResolutions = {
  cancel_agent_runs: number[];
  cancel_workflow_runs: number[];
  deactivate_workflows: number[];
};
export type ChatVisibility = "public" | "private";
export type ChatMemberKind = "user" | "agent" | "workflow";
/** The kinds of member that aren't users: the organisation's agents and workflows. */
export type ChatTargetKind = Exclude<ChatMemberKind, "user">;

/** Someone in a chat: a user (with a role), an agent or a workflow. */
export type ChatMember = {
  /** The membership's id, which removing the member takes. */
  id: number;
  kind: ChatMemberKind;
  /** Users only: the chat's owner, or an ordinary member. */
  role: "owner" | "member" | null;
  /** The user's, agent's or workflow's id. */
  target_id: number;
  /** A user's full name ("First Last"). */
  name: string;
  added_at: string;
  /** An archived agent: still a member, but not offered to mention. */
  archived: boolean;
};

/** Who made it and last changed it: the chat's owner role is separate (`is_owner`). */
export type ChatSummary = Provenance & {
  id: number;
  name: string;
  description: string;
  /** Public: every member of the organisation can find it. Private: it doesn't exist for anyone
   * who isn't in it. */
  visibility: ChatVisibility;
  /** Whether the signed-in user is its owner (the role on their member row): only the owner
   * edits, deletes or removes members. */
  is_owner: boolean;
  /** Whether the signed-in user is in it, and so can post: anyone can read a public chat. */
  is_member: boolean;
  member_count: number;
  created_at: string;
  /** When it was last active. */
  updated_at: string;
};

/** A chat as the list shows it: those the user is in, and the public ones they could join. */
export type ChatListed = ChatSummary & {
  /** Others' messages since the last one the user read; null for a chat they're not in. */
  unread_count: number | null;
};

export type Chat = ChatSummary & {
  /** Grouped by kind; the owner first, then in the order they were added. */
  members: { users: ChatMember[]; agents: ChatMember[]; workflows: ChatMember[] };
};

export type ChatInput = { name: string; description: string; visibility: ChatVisibility };

/** A member a message mentions: a user, an agent or a workflow, by id within its kind. */
export type ChatMentionRef = { kind: ChatMemberKind; id: number };
export type ChatMention = ChatMentionRef & { name: string };

/**
 * A member the composer offers to mention: a user's `name` is their full name, and their first
 * and last names (null for an agent or a workflow) are what's typed after `@` matches.
 */
export type ChatMentionable = ChatMention & { first_name: string | null; last_name: string | null };
/** Who can be mentioned in a chat (its members), grouped by kind. */
export type ChatMentionables = {
  users: ChatMentionable[];
  agents: ChatMentionable[];
  workflows: ChatMentionable[];
};

/** Who posted a chat message: a user (by full name), an agent or a workflow. */
export type ChatMessageAuthor = {
  kind: "user" | "agent" | "workflow" | "system";
  id: number;
  name: string;
};

export type ChatMessage = {
  id: number;
  chat_id: number;
  /** Null for a message from the chat itself, or once its author has been deleted. */
  author: ChatMessageAuthor | null;
  kind: "text" | "run_card";
  /** Markdown. */
  body: string;
  /** Who it mentions, as they were named when it was posted; its body has a token for each. */
  mentions: ChatMention[];
  /** The invocation it belongs to: a run card's (`run_card`), or the result its run posted back. */
  invocation_id: number | null;
  created_at: string;
};

/** queued: waiting to start; briefing: the handoff is briefing its run on the chat, which starts next. */
export type ChatInvocationStatus = "queued" | "briefing" | "running" | "succeeded" | "failed";

/** A run a mention in a chat started, as its card follows it. */
export type ChatInvocation = {
  id: number;
  chat_id: number;
  /** The message that mentioned it, and who posted it. */
  message_id: number;
  requested_by: number | null;
  target_kind: ChatTargetKind;
  /** The agent's or workflow's id. */
  target_id: number;
  status: ChatInvocationStatus;
  /** Why it failed: the run's error, or why it couldn't start. */
  error: string | null;
  /** Its acknowledgement's text as it is now: a placeholder until the handoff writes what the
   * member understood it's being asked to do. */
  acknowledgement: string;
  /** What the handoff told the run of the chat, before the message; null until it has. */
  brief: string | null;
  workflow_run_id: number | null;
  agent_run_id: number | null;
  /** Whether it started a run that has since been deleted. */
  run_deleted: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** The workflow run as its page shows it; null without one. */
  workflow_run: WorkflowRun | null;
  /** The agent run's status, times and counters so far; null without one. */
  agent_run: ChatAgentRun | null;
};

/** How an agent run a mention started is going, as its card shows it (elapsed ticks from
 * `started_at` on the client). */
export type ChatAgentRun = Pick<
  RunSummary,
  "id" | "status" | "started_at" | "finished_at" | "tool_call_count" | "input_tokens" | "output_tokens"
> & {
  /** Whether its turn ended waiting for a member to decide its schema change request. */
  waiting: boolean;
};

/**
 * Messages of a chat, oldest first, and the state of each invocation they belong to and of every
 * one in the chat still going.
 */
export type ChatMessages = { messages: ChatMessage[]; invocations: ChatInvocation[] };

/** Which page of messages to read: what's new `after` an id, the history `before` one, or the latest. */
export type ChatMessagesCursor = { after: number } | { before: number } | Record<string, never>;

// What the API answers (401) when a request's session has ended, or never started
// (`aios.auth.NOT_SIGNED_IN`).
const NOT_SIGNED_IN = "Not signed in";

let sessionEnded: (() => void) | undefined;

/**
 * While someone is signed in: what to do when a request finds their session has ended, e.g.
 * because their password was changed in another browser. Not called for other 401s, such as a
 * wrong current password on the profile forms. Pass nothing to stop.
 */
export function onSessionEnded(handler?: () => void): void {
  sessionEnded = handler;
}

/**
 * `fetch` for the API, for every request to it (`request`, and the chat transports). Same-origin,
 * so the browser attaches the session cookie; and a 401 for an ended session is reported to
 * `onSessionEnded`'s handler.
 */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const res = await fetch(input, { credentials: "same-origin", ...init });
  if (res.status === 401 && sessionEnded) {
    const detail = await res
      .clone()
      .json()
      .then((body) => body?.detail)
      .catch(() => undefined);
    if (detail === NOT_SIGNED_IN) sessionEnded();
  }
  return res;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init);
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
  me: () => request<Me>("/api/me"),
  register: (body: Registration) => request<Me>("/api/auth/register", json("POST", body)),
  login: (body: { email: string; password: string }) =>
    request<Me>("/api/auth/login", json("POST", body)),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  authOptions: () => request<AuthOptions>("/api/auth/options"),
  devUsers: () => request<Account[]>("/api/dev/users"),
  devSignIn: (userId: number) => request<Me>("/api/dev/session", json("POST", { user_id: userId })),
  /** Change the signed-in user's name; a name left out is kept. */
  updateMe: (body: { first_name?: string; last_name?: string }) =>
    request<Me>("/api/me", json("PATCH", body)),
  /** Change the email the signed-in user signs in with; it takes their current password. */
  changeEmail: (body: { email: string; current_password: string }) =>
    request<Me>("/api/me/email", json("PUT", body)),
  /** Change the signed-in user's password. It signs out their other sessions, not this one. */
  changePassword: (body: { current_password: string; new_password: string; new_password_confirm: string }) =>
    request<void>("/api/me/password", json("PUT", body)),
  getOrganisation: () => request<Organisation>("/api/organisation"),
  /** The creator only (a 403 otherwise); a name another organisation has is a 409. */
  renameOrganisation: (name: string) =>
    request<Organisation>("/api/organisation", json("PUT", { name })),
  /** Invitation links: the creator only (a 403 otherwise). */
  listInvites: () => request<Invite[]>("/api/organisation/invites"),
  makeInvite: (expiresInDays: number) =>
    request<NewInvite>("/api/organisation/invites", json("POST", { expires_in_days: expiresInDays })),
  revokeInvite: (id: string) =>
    request<void>(`/api/organisation/invites/${id}`, { method: "DELETE" }),
  /** No sign-in needed; an expired or revoked link is a 404. */
  getInvitation: (token: string) =>
    request<Invitation>(`/api/invites/${encodeURIComponent(token)}`),
  /** A 409 says why the user can't join (they're in another organisation). */
  /** From an empty personal organisation, deletes it; a 409 (`NotEmpty`'s detail when it has
   * things in it) when the user can't. */
  acceptInvitation: (token: string) =>
    request<Organisation>(`/api/invites/${encodeURIComponent(token)}/accept`, { method: "POST" }),
  /** A 409 for the last member, and for the creator while others remain; otherwise the new
   * personal organisation the user lands in. */
  leaveOrganisation: () => request<Organisation>("/api/organisation/leave", { method: "POST" }),
  getDataStore: () =>
    request<{ status: DataStoreStatus; usage: DataStoreUsage }>("/api/data-store"),
  getSettings: () => request<Settings>("/api/organisation/settings"),
  updateSettings: (body: { export_run_history?: boolean; embed_run_history?: boolean }) =>
    request<Settings>("/api/organisation/settings", json("PUT", body)),
  deleteExportedHistory: () =>
    request<void>("/api/organisation/settings/exported-history", { method: "DELETE" }),
  listDocuments: () => request<DocumentInfo[]>("/api/documents"),
  deleteDocument: (id: number) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),
  uploadDocument: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<DocumentInfo>("/api/documents", { method: "POST", body });
  },
  listSchemaChanges: () => request<SchemaChangeRequest[]>("/api/schema-changes"),
  getSchemaChange: (id: string) => request<SchemaChangeRequest>(`/api/schema-changes/${id}`),
  approveSchemaChange: (id: string, confirm?: string) =>
    request<SchemaChangeRequest>(
      `/api/schema-changes/${id}/approve`,
      // A request that drops anything takes its tables' names typed.
      confirm === undefined ? { method: "POST" } : json("POST", { confirm }),
    ),
  schemaChangeImpact: (id: string) =>
    request<{ removes: Removal[] }>(`/api/schema-changes/${id}/impact`),
  listDropped: () => request<DroppedObject[]>("/api/dropped"),
  restoreDropped: (id: number) =>
    request<DroppedObject>(`/api/dropped/${id}/restore`, { method: "POST" }),
  rejectSchemaChange: (id: string, note: string) =>
    request<SchemaChangeRequest>(`/api/schema-changes/${id}/reject`, json("POST", { note })),
  listNotifications: () => request<NotificationList>("/api/notifications"),
  markNotificationRead: (id: string) =>
    request<Notification>(`/api/notifications/${id}/read`, { method: "POST" }),
  search: (q: string, limit: number) =>
    request<SearchHit[]>(`/api/search?${new URLSearchParams({ q, limit: String(limit) })}`),
  listDataTables: () =>
    request<{ tables: DataTable[]; truncated: boolean }>("/api/data/tables"),
  getDataRows: (table: string, { offset, limit, sort, order }: DataRowsQuery) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (sort) params.set("sort", sort);
    if (sort && order) params.set("order", order);
    return request<DataRowsPage>(`/api/data/tables/${encodeURIComponent(table)}/rows?${params}`);
  },
  getDataValue: (table: string, row: string, column: string) =>
    request<{ value: string | null; truncated: boolean }>(
      `/api/data/tables/${encodeURIComponent(table)}/value?${new URLSearchParams({ row, column })}`,
    ),

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
  /** The agents that aren't archived; with `archived`, the archived ones instead. */
  listAgents: (archived = false) =>
    request<AgentSummary[]>(archived ? "/api/agents?archived=true" : "/api/agents"),
  archiveAgent: (id: number) => request<Agent>(`/api/agents/${id}/archive`, { method: "POST" }),
  unarchiveAgent: (id: number) =>
    request<Agent>(`/api/agents/${id}/unarchive`, { method: "POST" }),
  getAgentUsedBy: (id: number) => request<AgentUsedBy>(`/api/agents/${id}/used-by`),
  getAgent: (id: number) => request<Agent>(`/api/agents/${id}`),
  createAgent: (body: AgentInput) => request<Agent>("/api/agents", json("POST", body)),
  updateAgent: (id: number, body: AgentInput) =>
    request<Agent>(`/api/agents/${id}`, json("PUT", body)),
  getAgentDependents: (id: number) => request<AgentDependents>(`/api/agents/${id}/dependents`),
  /** Refused (409, see `parseDeleteRefusal`) until what depends on it is resolved. */
  deleteAgent: (id: number, resolutions?: AgentDeleteResolutions) =>
    request<void>(
      `/api/agents/${id}`,
      resolutions ? json("DELETE", resolutions) : { method: "DELETE" },
    ),
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
  createWorkflow: (body: WorkflowInput) =>
    request<Workflow>("/api/workflows", json("POST", body)),
  updateWorkflow: (id: number, body: WorkflowInput) =>
    request<Workflow>(`/api/workflows/${id}`, json("PUT", body)),
  deleteWorkflow: (id: number) => request<void>(`/api/workflows/${id}`, { method: "DELETE" }),
  listWorkflowVersions: (id: number) =>
    request<WorkflowVersionSummary[]>(`/api/workflows/${id}/versions`),
  // Also a deleted workflow's versions, which a past run's subworkflow node still pins.
  getWorkflowVersion: (id: number, version: number) =>
    request<WorkflowVersion>(`/api/workflows/${id}/versions/${version}`),
  restoreWorkflowVersion: (id: number, version: number) =>
    request<Workflow>(`/api/workflows/${id}/versions/${version}/restore`, { method: "POST" }),
  listWorkflowRuns: (id: number) => request<WorkflowRunSummary[]>(`/api/workflows/${id}/runs`),
  getWorkflowRun: (id: number, runId: number) =>
    request<WorkflowRunPage>(`/api/workflows/${id}/runs/${runId}`),
  /** The version the run pinned for the workflow node at `path` (node keys, outermost first). */
  getRunSubworkflow: (id: number, runId: number, path: string[]) =>
    request<WorkflowVersion>(
      `/api/workflows/${id}/runs/${runId}/subworkflow?${new URLSearchParams(path.map((key) => ["path", key]))}`,
    ),
  createWorkflowRun: (id: number, input: string) =>
    request<WorkflowRunSummary>(`/api/workflows/${id}/runs`, json("POST", { input })),
  deleteWorkflowRun: (id: number, runId: number) =>
    request<void>(`/api/workflows/${id}/runs/${runId}`, { method: "DELETE" }),

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

  listChats: () => request<ChatListed[]>("/api/chats"),
  getChat: (id: number) => request<Chat>(`/api/chats/${id}`),
  createChat: (body: ChatInput) => request<Chat>("/api/chats", json("POST", body)),
  updateChat: (id: number, body: ChatInput) =>
    request<Chat>(`/api/chats/${id}`, json("PUT", body)),
  deleteChat: (id: number) => request<void>(`/api/chats/${id}`, { method: "DELETE" }),
  removeChatMember: (id: number, memberId: number) =>
    request<void>(`/api/chats/${id}/members/${memberId}`, { method: "DELETE" }),
  listChatMessages: (id: number, cursor: ChatMessagesCursor = {}) =>
    request<ChatMessages>(
      `/api/chats/${id}/messages?${new URLSearchParams(
        Object.entries(cursor).map(([k, v]) => [k, String(v)]),
      )}`,
    ),
  postChatMessage: (id: number, body: string, mentions: ChatMentionRef[] = []) =>
    request<ChatMessages>(`/api/chats/${id}/messages`, json("POST", { body, mentions })),
  listChatMentionable: (id: number) => request<ChatMentionables>(`/api/chats/${id}/mentionable`),
  addChatMember: (id: number, member: ChatMentionRef) =>
    request<Chat>(`/api/chats/${id}/members`, json("POST", member)),
  joinChat: (id: number) => request<Chat>(`/api/chats/${id}/join`, { method: "POST" }),
  leaveChat: (id: number) => request<void>(`/api/chats/${id}/leave`, { method: "POST" }),
  /** Notes that the user has seen the chat's messages up to `messageId`, clearing their unread count. */
  markChatRead: (id: number, messageId: number) =>
    request<void>(`/api/chats/${id}/read`, json("POST", { message_id: messageId })),
};
