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
  /** They've left the organisation; what they made still names them. */
  former_member?: boolean;
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

/** What a member may do in an organisation (`lib/permissions` has what each may do). */
export type Role = "admin" | "editor" | "participant";

/** An organisation, as the Organisation page and creating one answer it. */
export type OrganisationSummary = {
  id: number;
  name: string;
  /** What links in the browser name it by (`/o/<slug>/`); a rename leaves it unchanged. */
  slug: string;
  /** Whether the signed-in user created it. */
  is_creator: boolean;
  /** The signed-in user's role in it. */
  role: Role;
};

/** One of the organisations the signed-in user is in, as `/api/me` lists them. */
export type Membership = {
  id: number;
  name: string;
  slug: string;
  /** Whether the signed-in user created it. */
  is_creator: boolean;
  /** Their role in it: what they may do there. */
  role: Role;
  /** When it's deleted, while its deletion is scheduled (every page shows a banner); else null. */
  deletes_at: string | null;
};

/** The signed-in user (`GET /api/me`), with every organisation they're in, the one joined most
 * recently first: none for a person who has neither created nor joined one, whom the app sends
 * to the welcome page. */
export type Me = Account & { organisations: Membership[] };

export type OrganisationMember = {
  user: User;
  role: Role;
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
  /** Its deletion, while one is scheduled; else null. */
  deletion: OrganisationDeletion | null;
  /** How long runs and chat history are kept; every member sees it, admins change it. */
  retention: Retention;
  /** The caps on turns and workflow runs; every member sees them, admins change them. */
  limits: OrganisationLimits;
};

/** Who may invoke an agent or workflow by mentioning it. */
export type InvocableBy = "everyone" | "editors" | "admins";

/** An agent's caps on each of its turns; null follows the organisation's. Costs are in GBP. */
export type TurnLimits = {
  turn_model_calls: number | null;
  turn_tokens: number | null;
  turn_cost: number | null;
};

/** A workflow's caps on each of its runs; null follows the organisation's. Costs are in GBP. */
export type RunLimits = {
  run_model_calls: number | null;
  run_cost: number | null;
};

export type Limits = TurnLimits & RunLimits;
export type LimitName = keyof Limits;

/** The deployment's defaults, the organisation's overrides (null keeps the default), and so
 * the caps in force, which agents and workflows follow unless they set their own. */
export type OrganisationLimits = {
  defaults: Limits;
  overrides: Limits;
  in_force: Limits;
};

export type UsageGroup = "agent" | "workflow" | "person" | "day";

/** Usage totals for one agent, workflow, person or day. */
export type UsageTotal = {
  /** The id, or the day (YYYY-MM-DD); null for usage with none (a schedule's run has no person). */
  key: number | string | null;
  name: string | null;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  embedded_texts: number;
  web_searches: number;
  sandbox_seconds: number;
  /** Estimated, in GBP; null if nothing in it was priced. */
  cost: number | null;
  /** Entries with no price. */
  unpriced: number;
};

export type UsageReport = {
  currency: "GBP";
  group: UsageGroup;
  start: string;
  end: string;
  totals: UsageTotal[];
};

/** How many days finished runs, and chats' messages, are kept (1 to 36,500); null keeps them
 * indefinitely, the default. A nightly job prunes what's older. */
export type Retention = {
  run_days: number | null;
  chat_days: number | null;
};

/** An organisation's scheduled deletion: it's deleted at `deletes_at` unless an admin cancels. */
export type OrganisationDeletion = {
  scheduled_at: string;
  /** Null once their account is deleted. */
  scheduled_by: User | null;
  deletes_at: string;
};

/** An open invitation link into the organisation, as its admins list them. */
export type Invite = {
  id: string;
  /** The role whoever joins through it is given. */
  role: Role;
  /** Null once their account is deleted. */
  creator: User | null;
  created_at: string;
  expires_at: string;
};

/** A link just made: the only time its token (and so the link) is shown. */
export type NewInvite = Invite & { token: string; url: string };

/** What the invite page shows of an open link, to anyone holding it. */
export type Invitation = { organisation: { id: number; name: string }; role: Role; expires_at: string };

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

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

/** Jev's verdict on stored content (`aios.jev.Verdict`): only clean, released and derived content
 * is searchable and reaches agents. */
export type Verdict = "awaiting_check" | "clean" | "flagged" | "released" | "derived";

/** A document Jev flagged, on the admins' flagged list. */
export type FlaggedDocument = {
  id: number;
  filename: string;
  /** Jev's reason. */
  reason: string | null;
  checked_at: string | null;
  uploaded_by: User | null;
  created_at: string;
};

/** What brings content into the store. Every store has exactly one `upload` source, Uploads. */
export type SourceKind = "upload" | "url" | "webhook" | "email" | "connector";
export type SourceStatus = "active" | "paused" | "failing" | "needs_reauth" | "removing";

export type Source = {
  id: number;
  kind: SourceKind;
  name: string;
  status: SourceStatus;
  /** The documents it has brought in. */
  document_count: number;
  created_at: string;
};

/** The source a document belongs to: Uploads for an upload. */
export type DocumentSource = Pick<Source, "id" | "kind" | "name">;

export type DocumentInfo = {
  id: number;
  filename: string;
  status: DocumentStatus;
  error: string | null;
  /** Jev's verdict: only a clean or released document is searchable. */
  verdict: Verdict;
  /** Why Jev flagged it. */
  verdict_reason: string | null;
  chunk_count: number;
  size_bytes: number;
  /** The member who uploaded it; null for documents from before it was recorded. */
  uploaded_by: User | null;
  source: DocumentSource;
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
/** An agent as a table's access names it. */
export type TableAgent = { id: number; name: string; archived: boolean };

/**
 * Who may write a table: its owner (null: no owner, so any agent with `write_data` may) and the
 * agents it's shared with. A table whose owner is archived is read-only until it's transferred.
 */
export type TableAccess = {
  name: string;
  owner: TableAgent | null;
  shares: TableAgent[];
  /** A change the database hasn't applied yet. */
  pending: boolean;
};

export type DataTable = TableAccess & {
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

/** What the search index holds: a document's passage, a chat message, a past run's message
 * (what the user or the assistant said), or the brief a handoff gave a run. */
/**
 * Something an agent remembered (its `remember` tool), as its Memory tab lists it: with where it
 * was learned (the run and turn that wrote it, and that run's chat, each null if there wasn't
 * one or it has since been pruned) and who last edited it (null: the agent wrote it as it is).
 */
export type AgentMemory = {
  id: number;
  content: string;
  verdict: "derived";
  agent_run_id: number | null;
  run_title: string | null;
  turn_id: number | null;
  chat_id: number | null;
  chat_name: string | null;
  created_at: string;
  updated_at: string;
  updated_by: User | null;
};

export type SearchKind = "document_chunk" | "chat_message" | "run_message" | "brief";

/** An item found by the unified hybrid (semantic + keyword) search, labelled with its kind and
 * linked to its source. */
export type SearchHit = {
  id: number;
  kind: SearchKind;
  text: string;
  created_at: string;
  /** A passage's headings and pages; a run message's role. */
  ref: { headings?: string[]; pages?: number[]; role?: "user" | "assistant" };
  document_id: number | null;
  filename: string | null;
  chat_id: number | null;
  chat_name: string | null;
  message_id: number | null;
  agent_id: number | null;
  agent_name: string | null;
  agent_run_id: number | null;
  run_title: string | null;
  /** Reciprocal Rank Fusion score: higher is better. */
  score: number;
  /** The item's rank in each branch, or null if that branch didn't find it. */
  semantic_rank: number | null;
  lexical_rank: number | null;
};

export type SkillSummary = Provenance & Archived & {
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

export type Skill = Provenance & Archived & {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
  current_version: SkillVersion;
  /** Agents whose current version uses some version of this skill. */
  agents: SkillUsage[];
};

export type SkillInput = { name: string; description: string; content: string; note?: string };

export type AgentSkill = {
  skill_id: number;
  skill_version_id: number;
  name: string;
  description: string;
  version: number;
  /** The skill's current version; null if the skill has been archived (there's nothing to
   * update to: a new agent version can't include it). */
  latest_version: number | null;
  latest_version_id: number | null;
  /** The skill has since been archived: versions pinning it still run, but a new version can't
   * include it. */
  archived: boolean;
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
  /** Why this version can't run here (a tool it needs is missing, say), or null if it can. */
  unrunnable: string | null;
};

/** When an agent, skill or workflow was archived and by whom: both null while it isn't. */
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
  invocable_by: InvocableBy;
  /** Its own caps (null follows the organisation's), and those in force for its turns. */
  limits: TurnLimits;
  limits_in_force: TurnLimits;
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
  /** Omitted: kept as they are. Only admins may set a cap above the organisation's (403). */
  invocable_by?: InvocableBy;
  limits?: TurnLimits;
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
   * counters, for a run with no turns yet (see `lib/agent-runs`). */
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
  /** The least role that may decide it: the highest its operations need (the approval rules). */
  needs_role: Role;
  /** Whether the signed-in user may approve or reject it (it's still pending). */
  can_decide: boolean;
  /** What approving it takes typed, if it drops anything: the tables' names. */
  confirm_with: string | null;
};

/** The least role that may decide a kind of schema change: an admin may raise it, never lower it
 * below its default. */
export type ApprovalRule = {
  kind: string;
  label: string;
  role: Role;
  default_role: Role;
  updated_at: string | null;
};

/** One entry of the organisation's audit log: who did what, to what, and when. */
export type AuditEntry = {
  id: number;
  at: string;
  actor: {
    kind: "person" | "agent" | "system";
    user: User | null;
    agent: { id: number; name: string | null } | null;
    run_id: number | null;
  };
  /** Dotted: "agent.archived", "approval.decided", "data.written". */
  action: string;
  target_kind: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
};

export type AuditPage = {
  entries: AuditEntry[];
  /** The cursor of the next, older, page; null on the last. */
  next: string | null;
};

/** What the audit log is filtered by: a person (user id), an action or its family ("agent"),
 * and days (YYYY-MM-DD, UTC), each left out to match any. */
export type AuditFilters = {
  actor?: number;
  action?: string;
  since?: string;
  until?: string;
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
  /** The organisation it's from (a person's notifications are from every organisation they're
   * in): it opens there. */
  organisation: { id: number; slug: string };
  /** What it's about: `schema_change_request` (a request waiting for a decision),
   * `organisation_deletion_scheduled`, `organisation_deletion_cancelled` or
   * `released_document_changed` (a document the user released has changed since). */
  kind: string;
  /** For a schema change request, `request_id` and `data_store_id`; for an organisation's
   * deletion, `organisation_name` (and, scheduled, `deletes_at`); for a released document,
   * `document_id`, `filename` and `data_store_id`. */
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

export type InvocationCause = "mention" | "schedule" | "manual" | "event";

/** Why a run ran: a mention, a schedule, an event trigger, or a person starting it by hand. A workflow agent
 * node's is a child of its workflow run's. */
export type InvocationRecord = {
  id: number;
  cause: InvocationCause;
  status: "queued" | "briefing" | "running" | "succeeded" | "failed";
  requested_by: number | null;
  trigger_id: number | null;
  parent_id: number | null;
  /** Its ancestors' ids, the root first; `depth` is how many. */
  ancestor_ids: number[];
  depth: number;
  chat_id: number | null;
  message_id: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

/** One turn of an agent run: the version it ran on, how it went, and what it cost. */
export type Turn = {
  id: number;
  invocation_id: number | null;
  agent_version_id: number;
  agent_version: number;
  status: AgentRunStatus;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
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
  /** Why it ran. */
  invocation: InvocationRecord | null;
  /** Its turns, oldest first. */
  turns: Turn[];
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
  /** What it declares, if anything: JSON Schemas for what it takes and gives (an agent node with
   * an output schema gives structured output), and the template its input is built from. */
  input_schema?: JsonValue | null;
  output_schema?: JsonValue | null;
  input_template?: JsonValue | null;
};

/** Any JSON value: a schema, a template, a condition, a structured input or output. */
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

/** `condition`: a JSONLogic condition on the source's output and the run's input; the edge is
 * active only when it holds. Absent: always, once the source succeeds. */
export type WorkflowEdge = { source: string; target: string; condition?: JsonValue | null };

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
  /** JSON Schemas for what its runs take and give, if it declares them. */
  input_schema?: JsonValue | null;
  output_schema?: JsonValue | null;
  /** Why a run of this version can't start (an agent it runs can't), or null if it can. */
  unrunnable: string | null;
};

/** One thing an import makes, or a dry run says it would. */
export type ImportedItem = {
  kind: "skill" | "agent" | "workflow";
  /** Its reference in the file. */
  ref: string;
  /** Its name in the file, and the name it's made with. */
  name: string;
  imported_name: string;
  renamed: boolean;
  /** Why its version can't run here, if it can't. */
  unrunnable: string | null;
  /** Its id once made; null on a dry run. */
  id: number | null;
};

export type ImportResult = {
  dry_run: boolean;
  /** The agent or workflow the file is of. */
  root: ImportedItem;
  items: ImportedItem[];
};

export type WorkflowSummary = Provenance & Archived & {
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

export type Workflow = Provenance & Archived & {
  id: number;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  current_version: WorkflowVersion;
  /** Saved positions by node key; nodes without one are laid out automatically. */
  layout: Record<string, NodePosition>;
  invocable_by: InvocableBy;
  /** Its own caps (null follows the organisation's), and those in force for its runs. */
  limits: RunLimits;
  limits_in_force: RunLimits;
};

export type WorkflowInput = {
  name: string;
  description: string;
  nodes: {
    key: string;
    kind: WorkflowNodeKind;
    version_id: number;
    input_schema?: JsonValue | null;
    output_schema?: JsonValue | null;
    input_template?: JsonValue | null;
  }[];
  edges: WorkflowEdge[];
  input_schema?: JsonValue | null;
  output_schema?: JsonValue | null;
  /** Omitted: keep the saved layout. */
  layout?: Record<string, NodePosition>;
  note?: string;
  /** The version the edit started from; a 409 if the workflow has moved on. Omitted: no check. */
  base_version?: number;
  /** Omitted: kept as they are. Only admins may set a cap above the organisation's (403). */
  invocable_by?: InvocableBy;
  limits?: RunLimits;
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
  /** Its input as JSON (with an input schema), and its output as JSON: one final node's, or an
   * object of several's by node. */
  input_json?: JsonValue | null;
  output_json?: JsonValue | null;
  /** Why a run that succeeded has no output: "No final step ran". */
  output_note?: string | null;
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
   * whose nodes waits. skipped: no active path reached it (it never ran). */
  status: "running" | "waiting" | "succeeded" | "failed" | "skipped";
  input: string;
  output: string | null;
  error: string | null;
  /** Its input and output as JSON, when structured. */
  input_json?: JsonValue | null;
  output_json?: JsonValue | null;
  /** Once it succeeded: how its outgoing edges' conditions came out, by target. */
  conditions?: Record<string, boolean> | null;
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
/** How a trigger last went: its run's status, or why the loop rules refused an event trigger's
 * firing (`refused`; `rate_limited` past its rate). */
export type TriggerStatus = "running" | "succeeded" | "failed" | "refused" | "rate_limited";
export type TriggerKind = "schedule" | "event";
/** What happened in the store, as its outbox records it. */
export type EventType = "document_ingested" | "table_written" | "schema_change_applied" | "chat_message";
/** What an event trigger's event must match: document ingested triggers filter by source ids and
 * content types (`text/*` matches every text type). Each field left out matches anything. */
export type TriggerFilter = { sources?: number[]; content_types?: string[] };

/** Starts an agent or a workflow by itself: on a schedule (cron), or on an event in the store. */
export type Trigger = Provenance & {
  id: number;
  kind: TriggerKind;
  target_kind: TriggerTargetKind;
  target_id: number;
  target_name: string;
  enabled: boolean;
  /** Schedules: the message an agent's run starts with, or a workflow run's input. Event
   * triggers: what an agent's run is told besides the event, or a workflow input's template. */
  input: string;
  /** Schedules only. */
  cron: string | null;
  /** When it fires, in plain English: the schedule, or the event. */
  description: string;
  timezone: string | null;
  /** Event triggers only. */
  event_type: EventType | null;
  filter: TriggerFilter;
  fire_on_own_events: boolean;
  /** Why the platform turned it off, if it did. */
  disabled_reason: string | null;
  /** Null while disabled, and for event triggers. */
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
  kind: TriggerKind;
  target_kind: TriggerTargetKind;
  target_id: number;
  enabled: boolean;
  input: string;
  cron: string | null;
  timezone: string;
  event_type?: EventType | null;
  filter?: TriggerFilter;
  fire_on_own_events?: boolean;
};

export type SchedulePreview = { description: string; next_runs: string[] };

/** A trigger that starts runs of something an archive confirmation lists. */
export type UsedByTrigger = {
  id: number;
  enabled: boolean;
  /** Schedules only. */
  cron: string | null;
  description: string;
  timezone: string | null;
  next_run_at: string | null;
};

/** A live workflow whose current version runs an agent or nests a workflow, at any depth. */
export type UsedByWorkflow = {
  id: number;
  name: string;
  version: number;
  /** Whether one of its own nodes does, rather than a nested workflow's. */
  direct: boolean;
  /** Its schedules. */
  triggers: UsedByTrigger[];
};

/** What uses an agent, as its archive confirmation shows it (GET /api/agents/{id}/used-by). */
export type AgentUsedBy = {
  /** Live workflows that run it at any depth, with their schedules. */
  workflows: UsedByWorkflow[];
  /** Its own schedules, which archiving pauses. */
  triggers: UsedByTrigger[];
  /** The chats it's a member of. */
  chats: { id: number; name: string }[];
  /** The data tables it owns, which archiving it leaves read-only. */
  tables: { name: string }[];
};

/** What uses a workflow, as its archive confirmation shows it (GET /api/workflows/{id}/used-by). */
export type WorkflowUsedBy = Omit<AgentUsedBy, "tables">;

/** An agent whose current version uses some version of a skill. */
export type SkillUsage = {
  agent_id: number;
  agent_name: string;
  agent_version: number;
  skill_version: number;
};

/** What uses a skill, as its archive confirmation shows it (GET /api/skills/{id}/used-by). */
export type SkillUsedBy = { agents: SkillUsage[] };

export type ChatMemberKind = "user" | "agent" | "workflow";
/** The kinds of member that aren't users: the organisation's agents and workflows. */
export type ChatTargetKind = Exclude<ChatMemberKind, "user">;

/** Someone in a chat: a user, an agent or a workflow. */
export type ChatMember = {
  /** The membership's id, which removing the member takes. */
  id: number;
  kind: ChatMemberKind;
  /** The user's, agent's or workflow's id. */
  target_id: number;
  /** A user's full name ("First Last"). */
  name: string;
  added_at: string;
  /** An archived agent or workflow: still a member, but not offered to mention. */
  archived: boolean;
};

/**
 * What a person may do to a chat, by the chat rules: post (joining, if they haven't), join, leave,
 * edit (rename or describe), include (add or remove its agents and workflows), and, for admins,
 * archive or unarchive. Nobody adds or removes a person, or deletes a chat.
 */
export type ChatAction = "post" | "join" | "leave" | "edit" | "include" | "archive" | "unarchive";

/** An open chat: everyone in the organisation can find and read it, and posting joins it. Chats
 * have no owners; an admin may archive one, which makes it read-only (still read and searched). */
export type ChatSummary = Provenance & Archived & {
  id: number;
  name: string;
  description: string;
  /** Whether the signed-in user has joined it: its members change it and its agents and
   * workflows, and count what's unread. */
  is_member: boolean;
  member_count: number;
  created_at: string;
  /** When it was last active. */
  updated_at: string;
  /** What the signed-in user may do to it now: the screens show these and hide the rest. */
  actions: ChatAction[];
};

/** A chat as the list shows it: every chat of the organisation, joined or not. */
export type ChatListed = ChatSummary & {
  /** Others' messages since the last one the user read; null for a chat they're not in. */
  unread_count: number | null;
};

export type Chat = ChatSummary & {
  /** Grouped by kind, in the order they were added. */
  members: { users: ChatMember[]; agents: ChatMember[]; workflows: ChatMember[] };
};

export type ChatInput = { name: string; description: string };

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

/** queued: waiting to start; briefing: the handoff is briefing its run on the chat, which starts next;
 * refused: the poster may not invoke it (its invocable-by), so nothing ran. */
export type ChatInvocationStatus = "queued" | "briefing" | "running" | "succeeded" | "failed" | "refused";

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
  /** Why the mention was refused: who may invoke it. */
  refusal_reason: string | null;
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

let organisationId: number | null = null;

/**
 * Open the page in an organisation (by its id, as the API names it): every organisation-scoped
 * request from then on goes under its prefix (`organisationApi`). The organisation is the
 * router's basename (`lib/organisation-path`), fixed for as long as the page is open, so two tabs
 * on two organisations each send their own.
 */
export function openOrganisationApi(id: number): void {
  organisationId = id;
}

/**
 * The API's prefix for the organisation the page is open in, `/api/o/<id>`: what every
 * organisation-scoped path starts with (`${organisationApi()}/agents`). Only the person's own
 * routes are outside it: signing in, the profile, creating an organisation, invitations,
 * notifications, the tool library and the list of timezones.
 */
export function organisationApi(): string {
  if (organisationId === null) throw new Error("No organisation is open on this page");
  return `/api/o/${organisationId}`;
}

/** The id of the organisation the page is open in (`openOrganisationApi`), if one is. */
export function openOrganisationId(): number | null {
  return organisationId;
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
      // A refusal that says more than why (a chat post's, `postRefusal`).
      if (typeof detail?.message === "string") return detail.message;
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

/** The audit log's query string: the filters given, and the page's cursor. */
export function auditQuery(filters: AuditFilters, before?: string): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.actor !== undefined) params.set("actor", String(filters.actor));
  if (filters.action?.trim()) params.set("action", filters.action.trim());
  if (filters.since) params.set("since", filters.since);
  if (filters.until) params.set("until", filters.until);
  if (before) params.set("before", before);
  return params;
}

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
  getOrganisation: () => request<Organisation>(`${organisationApi()}/organisation`),
  /** Admins only (a 403 otherwise). Names may repeat. */
  renameOrganisation: (name: string) =>
    request<Organisation>(`${organisationApi()}/organisation`, json("PUT", { name })),
  /** Admins only: how long runs and chat history are kept (null: indefinitely). */
  setRetention: (retention: Retention) =>
    request<Organisation>(`${organisationApi()}/organisation/retention`, json("PUT", retention)),
  /** Admins only: override the deployment's caps (null: the default). */
  setLimits: (limits: Limits) =>
    request<Organisation>(`${organisationApi()}/organisation/limits`, json("PUT", limits)),
  /** Admins only: usage totals by agent, workflow, person or day, from `start` to `end`. */
  usage: (group: UsageGroup, start?: string, end?: string) => {
    const params = new URLSearchParams({ group });
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    return request<UsageReport>(`${organisationApi()}/usage?${params}`);
  },
  /** Admins only (a 403 otherwise); demoting the last admin is a 409. Answers with the
   * Organisation page. */
  changeRole: (userId: number, role: Role) =>
    request<Organisation>(`${organisationApi()}/organisation/members/${userId}/role`, json("PUT", { role })),
  /** Invitation links: admins only (a 403 otherwise). */
  listInvites: () => request<Invite[]>(`${organisationApi()}/organisation/invites`),
  makeInvite: (expiresInDays: number, role: Role = "editor") =>
    request<NewInvite>(
      `${organisationApi()}/organisation/invites`,
      json("POST", { expires_in_days: expiresInDays, role }),
    ),
  revokeInvite: (id: string) =>
    request<void>(`${organisationApi()}/organisation/invites/${id}`, { method: "DELETE" }),
  /** No sign-in needed; an expired or revoked link is a 404. */
  getInvitation: (token: string) =>
    request<Invitation>(`/api/invites/${encodeURIComponent(token)}`),
  /** Adds a membership of the link's organisation, whatever else the user is in, and answers
   * with it; the same again for one they're in already. */
  acceptInvitation: (token: string) =>
    request<Organisation>(`/api/invites/${encodeURIComponent(token)}/accept`, { method: "POST" }),
  /** A new organisation, with the user as its creator and only member, and its store being
   * provisioned. Names may repeat: the slug tells organisations apart. */
  createOrganisation: (name: string) => request<Organisation>("/api/organisations", json("POST", { name })),
  /** A 409 for the last member, and for the last admin while others remain; otherwise the user, as
   * `me` answers, with the organisations they're still in. */
  leaveOrganisation: () => request<Me>(`${organisationApi()}/organisation/leave`, { method: "POST" }),
  /** Admins only (a 403 otherwise), typing the organisation's name (another is a 422); a 409
   * if it's scheduled already. Every member is notified. */
  scheduleOrganisationDeletion: (name: string) =>
    request<Organisation>(`${organisationApi()}/organisation/deletion`, json("POST", { name })),
  /** Admins only; a 409 if no deletion is scheduled. Every member is notified again. */
  cancelOrganisationDeletion: () =>
    request<Organisation>(`${organisationApi()}/organisation/deletion`, { method: "DELETE" }),
  getDataStore: () =>
    request<{ status: DataStoreStatus; usage: DataStoreUsage }>(`${organisationApi()}/data-store`),
  listDocuments: () => request<DocumentInfo[]>(`${organisationApi()}/documents`),
  /** Every member may read them; only admins change them, and never Uploads (a 409). */
  listSources: () => request<Source[]>(`${organisationApi()}/sources`),
  deleteDocument: (id: number) => request<void>(`${organisationApi()}/documents/${id}`, { method: "DELETE" }),
  uploadDocument: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<DocumentInfo>(`${organisationApi()}/documents`, { method: "POST", body });
  },
  listSchemaChanges: () => request<SchemaChangeRequest[]>(`${organisationApi()}/schema-changes`),
  getSchemaChange: (id: string) => request<SchemaChangeRequest>(`${organisationApi()}/schema-changes/${id}`),
  approveSchemaChange: (id: string, confirm?: string) =>
    request<SchemaChangeRequest>(
      `${organisationApi()}/schema-changes/${id}/approve`,
      // A request that drops anything takes its tables' names typed.
      confirm === undefined ? { method: "POST" } : json("POST", { confirm }),
    ),
  schemaChangeImpact: (id: string) =>
    request<{ removes: Removal[] }>(`${organisationApi()}/schema-changes/${id}/impact`),
  listFlagged: () => request<FlaggedDocument[]>(`${organisationApi()}/flagged`),
  releaseFlagged: (id: number) =>
    request<{ id: number; verdict: Verdict }>(`${organisationApi()}/flagged/${id}/release`, { method: "POST" }),
  jevStatus: () => request<{ up: boolean | null }>(`${organisationApi()}/jev-status`),
  listApprovalRules: () => request<ApprovalRule[]>(`${organisationApi()}/approval-rules`),
  setApprovalRule: (kind: string, role: Role) =>
    request<ApprovalRule[]>(`${organisationApi()}/approval-rules/${kind}`, json("PUT", { role })),
  auditLog: (filters: AuditFilters, before?: string) =>
    request<AuditPage>(`${organisationApi()}/audit-log?${auditQuery(filters, before)}`),
  listDropped: () => request<DroppedObject[]>(`${organisationApi()}/dropped`),
  restoreDropped: (id: number) =>
    request<DroppedObject>(`${organisationApi()}/dropped/${id}/restore`, { method: "POST" }),
  rejectSchemaChange: (id: string, note: string) =>
    request<SchemaChangeRequest>(`${organisationApi()}/schema-changes/${id}/reject`, json("POST", { note })),
  listNotifications: () => request<NotificationList>("/api/notifications"),
  markNotificationRead: (id: string) =>
    request<Notification>(`/api/notifications/${id}/read`, { method: "POST" }),
  /** Every kind if `kinds` is empty. */
  search: (q: string, limit: number, kinds: SearchKind[] = []) =>
    request<SearchHit[]>(
      `${organisationApi()}/search?${new URLSearchParams([
        ["q", q],
        ["limit", String(limit)],
        ...kinds.map((k) => ["kind", k]),
      ])}`,
    ),
  listDataTables: () =>
    request<{ tables: DataTable[]; truncated: boolean }>(`${organisationApi()}/data/tables`),
  getDataRows: (table: string, { offset, limit, sort, order }: DataRowsQuery) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (sort) params.set("sort", sort);
    if (sort && order) params.set("order", order);
    return request<DataRowsPage>(`${organisationApi()}/data/tables/${encodeURIComponent(table)}/rows?${params}`);
  },
  /** Assign, transfer or (with null) clear a table's owner. */
  setTableOwner: (table: string, agentId: number | null) =>
    request<TableAccess>(
      `${organisationApi()}/data/tables/${encodeURIComponent(table)}/owner`,
      json("PUT", { agent_id: agentId }),
    ),
  shareTable: (table: string, agentId: number) =>
    request<TableAccess>(
      `${organisationApi()}/data/tables/${encodeURIComponent(table)}/shares`,
      json("POST", { agent_id: agentId }),
    ),
  unshareTable: (table: string, agentId: number) =>
    request<TableAccess>(`${organisationApi()}/data/tables/${encodeURIComponent(table)}/shares/${agentId}`, {
      method: "DELETE",
    }),
  getDataValue: (table: string, row: string, column: string) =>
    request<{ value: string | null; truncated: boolean }>(
      `${organisationApi()}/data/tables/${encodeURIComponent(table)}/value?${new URLSearchParams({ row, column })}`,
    ),

  /** The skills that aren't archived; with `archived`, the archived ones instead. */
  listSkills: (archived = false) =>
    request<SkillSummary[]>(`${organisationApi()}/skills${archived ? "?archived=true" : ""}`),
  getSkill: (id: number) => request<Skill>(`${organisationApi()}/skills/${id}`),
  createSkill: (body: SkillInput) => request<Skill>(`${organisationApi()}/skills`, json("POST", body)),
  updateSkill: (id: number, body: SkillInput) =>
    request<Skill>(`${organisationApi()}/skills/${id}`, json("PUT", body)),
  archiveSkill: (id: number) => request<Skill>(`${organisationApi()}/skills/${id}/archive`, { method: "POST" }),
  unarchiveSkill: (id: number) =>
    request<Skill>(`${organisationApi()}/skills/${id}/unarchive`, { method: "POST" }),
  getSkillUsedBy: (id: number) => request<SkillUsedBy>(`${organisationApi()}/skills/${id}/used-by`),
  listSkillVersions: (id: number) =>
    request<SkillVersionSummary[]>(`${organisationApi()}/skills/${id}/versions`),
  getSkillVersion: (id: number, version: number) =>
    request<SkillVersion>(`${organisationApi()}/skills/${id}/versions/${version}`),
  restoreSkillVersion: (id: number, version: number) =>
    request<Skill>(`${organisationApi()}/skills/${id}/versions/${version}/restore`, { method: "POST" }),
  listTools: () => request<ToolInfo[]>("/api/tools"),
  /** The agents that aren't archived; with `archived`, the archived ones instead. */
  listAgents: (archived = false) =>
    request<AgentSummary[]>(archived ? `${organisationApi()}/agents?archived=true` : `${organisationApi()}/agents`),
  archiveAgent: (id: number) => request<Agent>(`${organisationApi()}/agents/${id}/archive`, { method: "POST" }),
  unarchiveAgent: (id: number) =>
    request<Agent>(`${organisationApi()}/agents/${id}/unarchive`, { method: "POST" }),
  /** Where an agent version's export downloads from (its current version by default). */
  agentExportUrl: (id: number, version?: number) =>
    `${organisationApi()}/agents/${id}/export${version === undefined ? "" : `?version=${version}`}`,
  /** Where a workflow version's export downloads from (its current version by default). */
  workflowExportUrl: (id: number, version?: number) =>
    `${organisationApi()}/workflows/${id}/export${version === undefined ? "" : `?version=${version}`}`,
  importFile: (file: File, dryRun: boolean) => {
    const body = new FormData();
    body.append("file", file);
    return request<ImportResult>(
      `${organisationApi()}/imports${dryRun ? "?dry_run=true" : ""}`,
      { method: "POST", body },
    );
  },
  getAgentUsedBy: (id: number) => request<AgentUsedBy>(`${organisationApi()}/agents/${id}/used-by`),
  getAgent: (id: number) => request<Agent>(`${organisationApi()}/agents/${id}`),
  createAgent: (body: AgentInput) => request<Agent>(`${organisationApi()}/agents`, json("POST", body)),
  updateAgent: (id: number, body: AgentInput) =>
    request<Agent>(`${organisationApi()}/agents/${id}`, json("PUT", body)),
  listAgentVersions: (id: number) =>
    request<AgentVersionSummary[]>(`${organisationApi()}/agents/${id}/versions`),
  getAgentVersion: (id: number, version: number) =>
    request<AgentVersion>(`${organisationApi()}/agents/${id}/versions/${version}`),
  restoreAgentVersion: (id: number, version: number) =>
    request<Agent>(`${organisationApi()}/agents/${id}/versions/${version}/restore`, { method: "POST" }),
  listRuns: (agentId: number) => request<RunSummary[]>(`${organisationApi()}/agents/${agentId}/runs`),
  getRun: (agentId: number, runId: number) =>
    request<Run>(`${organisationApi()}/agents/${agentId}/runs/${runId}`),
  deleteRun: (agentId: number, runId: number) =>
    request<void>(`${organisationApi()}/agents/${agentId}/runs/${runId}`, { method: "DELETE" }),
  listMemories: (agentId: number) =>
    request<AgentMemory[]>(`${organisationApi()}/agents/${agentId}/memories`),
  editMemory: (agentId: number, memoryId: number, content: string) =>
    request<AgentMemory>(
      `${organisationApi()}/agents/${agentId}/memories/${memoryId}`,
      json("PATCH", { content }),
    ),
  deleteMemory: (agentId: number, memoryId: number) =>
    request<void>(`${organisationApi()}/agents/${agentId}/memories/${memoryId}`, { method: "DELETE" }),
  /** The workflows that aren't archived; with `archived`, the archived ones instead. */
  listWorkflows: (archived = false) =>
    request<WorkflowSummary[]>(`${organisationApi()}/workflows${archived ? "?archived=true" : ""}`),
  getWorkflow: (id: number) => request<Workflow>(`${organisationApi()}/workflows/${id}`),
  createWorkflow: (body: WorkflowInput) =>
    request<Workflow>(`${organisationApi()}/workflows`, json("POST", body)),
  updateWorkflow: (id: number, body: WorkflowInput) =>
    request<Workflow>(`${organisationApi()}/workflows/${id}`, json("PUT", body)),
  archiveWorkflow: (id: number) =>
    request<Workflow>(`${organisationApi()}/workflows/${id}/archive`, { method: "POST" }),
  unarchiveWorkflow: (id: number) =>
    request<Workflow>(`${organisationApi()}/workflows/${id}/unarchive`, { method: "POST" }),
  getWorkflowUsedBy: (id: number) =>
    request<WorkflowUsedBy>(`${organisationApi()}/workflows/${id}/used-by`),
  listWorkflowVersions: (id: number) =>
    request<WorkflowVersionSummary[]>(`${organisationApi()}/workflows/${id}/versions`),
  // Also an archived workflow's versions, which a past run's subworkflow node still pins.
  getWorkflowVersion: (id: number, version: number) =>
    request<WorkflowVersion>(`${organisationApi()}/workflows/${id}/versions/${version}`),
  restoreWorkflowVersion: (id: number, version: number) =>
    request<Workflow>(`${organisationApi()}/workflows/${id}/versions/${version}/restore`, { method: "POST" }),
  listWorkflowRuns: (id: number) => request<WorkflowRunSummary[]>(`${organisationApi()}/workflows/${id}/runs`),
  getWorkflowRun: (id: number, runId: number) =>
    request<WorkflowRunPage>(`${organisationApi()}/workflows/${id}/runs/${runId}`),
  /** The version the run pinned for the workflow node at `path` (node keys, outermost first). */
  getRunSubworkflow: (id: number, runId: number, path: string[]) =>
    request<WorkflowVersion>(
      `${organisationApi()}/workflows/${id}/runs/${runId}/subworkflow?${new URLSearchParams(path.map((key) => ["path", key]))}`,
    ),
  /** Text input, or JSON for a version with an input schema (`{ json }`). */
  createWorkflowRun: (id: number, input: string | { json: JsonValue }) =>
    request<WorkflowRunSummary>(
      `${organisationApi()}/workflows/${id}/runs`,
      json("POST", typeof input === "string" ? { input } : { input_json: input.json }),
    ),
  deleteWorkflowRun: (id: number, runId: number) =>
    request<void>(`${organisationApi()}/workflows/${id}/runs/${runId}`, { method: "DELETE" }),

  listTriggers: (target?: { kind: TriggerTargetKind; id: number }) =>
    request<Trigger[]>(
      `${organisationApi()}/triggers` +
        (target ? `?${new URLSearchParams({ target_kind: target.kind, target_id: String(target.id) })}` : ""),
    ),
  getTrigger: (id: number) => request<Trigger>(`${organisationApi()}/triggers/${id}`),
  createTrigger: (body: TriggerInput) => request<Trigger>(`${organisationApi()}/triggers`, json("POST", body)),
  updateTrigger: (id: number, body: TriggerInput) =>
    request<Trigger>(`${organisationApi()}/triggers/${id}`, json("PUT", body)),
  deleteTrigger: (id: number) => request<void>(`${organisationApi()}/triggers/${id}`, { method: "DELETE" }),
  previewSchedule: (cron: string, timezone: string) =>
    request<SchedulePreview>(`${organisationApi()}/triggers/preview`, json("POST", { cron, timezone })),
  listTimezones: () => request<string[]>("/api/triggers/timezones"),

  /** The chats that aren't archived; with `archived`, the archived ones instead. */
  listChats: (archived = false) =>
    request<ChatListed[]>(archived ? `${organisationApi()}/chats?archived=true` : `${organisationApi()}/chats`),
  getChat: (id: number) => request<Chat>(`${organisationApi()}/chats/${id}`),
  createChat: (body: ChatInput) => request<Chat>(`${organisationApi()}/chats`, json("POST", body)),
  /** **Chat with this agent**: a new chat with that agent only, named for it and the person
   * ("<Agent> – <person> – <date>", " (2)" and so on on a clash); a 403 saying who may invoke it. */
  chatWithAgent: (agentId: number) =>
    request<Chat>(`${organisationApi()}/chats`, json("POST", { agent_id: agentId })),
  updateChat: (id: number, body: ChatInput) =>
    request<Chat>(`${organisationApi()}/chats/${id}`, json("PUT", body)),
  archiveChat: (id: number) => request<Chat>(`${organisationApi()}/chats/${id}/archive`, { method: "POST" }),
  unarchiveChat: (id: number) => request<Chat>(`${organisationApi()}/chats/${id}/unarchive`, { method: "POST" }),
  removeChatMember: (id: number, memberId: number) =>
    request<void>(`${organisationApi()}/chats/${id}/members/${memberId}`, { method: "DELETE" }),
  listChatMessages: (id: number, cursor: ChatMessagesCursor = {}) =>
    request<ChatMessages>(
      `${organisationApi()}/chats/${id}/messages?${new URLSearchParams(
        Object.entries(cursor).map(([k, v]) => [k, String(v)]),
      )}`,
    ),
  postChatMessage: (id: number, body: string, mentions: ChatMentionRef[] = []) =>
    request<ChatMessages>(`${organisationApi()}/chats/${id}/messages`, json("POST", { body, mentions })),
  listChatMentionable: (id: number) => request<ChatMentionables>(`${organisationApi()}/chats/${id}/mentionable`),
  addChatMember: (id: number, member: ChatMentionRef) =>
    request<Chat>(`${organisationApi()}/chats/${id}/members`, json("POST", member)),
  joinChat: (id: number) => request<Chat>(`${organisationApi()}/chats/${id}/join`, { method: "POST" }),
  leaveChat: (id: number) => request<void>(`${organisationApi()}/chats/${id}/leave`, { method: "POST" }),
  /** The chat's live events (`lib/stream`): its messages, run cards, drafts, header and members. */
  chatStreamUrl: (id: number) => `${organisationApi()}/chats/${id}/stream`,
  /** The person's live chat list events, across every organisation they're in (`lib/stream`). */
  personStreamUrl: () => "/api/me/stream",
  /** Notes that the user has seen the chat's messages up to `messageId`, clearing their unread count. */
  markChatRead: (id: number, messageId: number) =>
    request<void>(`${organisationApi()}/chats/${id}/read`, json("POST", { message_id: messageId })),
};
