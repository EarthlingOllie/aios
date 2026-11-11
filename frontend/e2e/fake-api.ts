import type { Page, Route } from "@playwright/test";

/**
 * An in-memory stand-in for the backend's agents and workflows API, enough for the browser
 * tests: it keeps state across requests, so a test can save and see the result, and follows
 * the server's rules (a changed graph is a new version, the layout isn't versioned, nodes pin
 * versions that must exist, a workflow can't contain itself, a save based on an older version
 * is refused), plus schedules' preview and timezones, and chats with their members (a chat of
 * another organisation is a 404, as a private chat is for a non-member; a public one is listed,
 * readable and joined by any member of the organisation, and left by any member but its owner
 * (the role on their member row); the owner's actions are a 403 for anyone else; names are unique within the organisation), each
 * saying who made it and last changed it, and each with an
 * unread count of others' messages since the last one read (`chatReads`), and messages
 * (read a page at a time by id, `after` or `before` a cursor; only members post; a mention of
 * anyone who isn't a member is a 422). Mentioning a workflow or an agent starts a scripted
 * invocation: its acknowledgement and card are posted with the message, and each read of the
 * chat's messages moves it on (`advanceInvocations`): briefing, then the handoff's ack and brief
 * (`handoffs`, by member id) and its run, until the run ends and the result is posted back. An
 * agent's turn takes `agentTurns` steps (by agent id; `AGENT_TURN` by default), each a read
 * adding a tool call and tokens to its run's counters. A chat made through the API has the
 * documents assistant (`documentsAssistant`) as its first agent; one added with `addChat` hasn't.
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
  /** The keys of the tools its current version has (`addToolUse`). */
  tools: string[];
  /** The members who made it and last changed it (`me` unless a test says otherwise). */
  created_by: ChatUser | null;
  updated_by: ChatUser | null;
  /** When it was archived, and by whom: both null while it isn't. */
  archived_at: string | null;
  archived_by: ChatUser | null;
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
  /** Whether the signed-in user may decide it (any member of the store's organisation), while
   * it's pending. */
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
  /** If set, a restore is accepted but never happens (the worker gave up without a word). */
  stallRestore?: boolean;
};
export type NotificationRow = {
  id: string;
  /** The organisation it's from: the fake's own unless a test says otherwise. */
  organisation: { id: number; slug: string };
  kind: string;
  ref: Record<string, string>;
  /** For a kind other than a schema change request (whose title is its summary). */
  title: string | null;
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
};

export type DocumentRow = {
  id: number;
  filename: string;
  status: "pending" | "processing" | "ready" | "failed";
  error: string | null;
  chunk_count: number;
  size_bytes: number;
  /** The member who uploaded it; null for one from before uploads were recorded. */
  uploaded_by: ChatUser | null;
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
};
/** As the server cuts values in a page of rows (`PREVIEW_CHARS`). */
const PREVIEW_CHARS = 200;
type RunStatus = "queued" | "running" | "waiting" | "succeeded" | "failed";
type StepStatus = "running" | "waiting" | "succeeded" | "failed";
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
  started_at: string;
  finished_at: string | null;
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
export type ChatUser = Pick<Account, "id" | "first_name" | "last_name">;
type ChatVisibility = "public" | "private";
export type ChatMemberRow =
  | {
      id: number;
      kind: "user";
      role: "owner" | "member";
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
  role: "owner" | "member" | null;
  target_id: number;
  name: string;
  added_at: string;
  archived: boolean;
};
export type OrganisationMemberRow = { user: ChatUser; joined_at: string; invite_id: string | null };
/** An open invitation link, with the token the fake keeps (the API keeps only its hash). */
export type InviteRow = { id: string; creator: ChatUser | null; created_at: string; expires_at: string; token: string };
export type InvitationRow = { organisation: { id: number; name: string }; expires_at: string };
export type OrganisationRow = {
  id: number;
  name: string;
  /** What links name it by (`/o/<slug>/`); a rename leaves it unchanged. */
  slug: string;
  personal: boolean;
  creator: ChatUser | null;
  members: OrganisationMemberRow[];
  created_at: string;
};

export type ChatRow = {
  id: number;
  /** The organisation it belongs to: `organisation`'s unless a test says otherwise. */
  organisation_id: number;
  created_by: ChatUser | null;
  updated_by: ChatUser | null;
  name: string;
  description: string;
  visibility: ChatVisibility;
  members: ChatMemberRow[];
  /** Oldest first. */
  messages: ChatMessageRow[];
  created_at: string;
  updated_at: string;
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
type InvocationStatus = "queued" | "briefing" | "running" | "succeeded" | "failed";
/** An agent's or workflow's run that a mention started. */
export type InvocationRow = {
  id: number;
  chat_id: number;
  message_id: number;
  requested_by: number;
  target_kind: Kind;
  target_id: number;
  status: InvocationStatus;
  error: string | null;
  /** What the handoff told its run of the chat; null until it has. */
  brief: string | null;
  /** The run it started; gone from `runs` (or `agentRuns`) once it's deleted. */
  run_id: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

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
export type AgentTurn = { steps: number; tokensPerStep: number; reply?: string; error?: string };
export const AGENT_TURN: AgentTurn = { steps: 2, tokensPerStep: 120 };

/** A message's text: each mention as `@name`. */
const messageText = (body: string) =>
  body.replace(/@\[((?:\\.|[^\\\]])*)\]\((?:user|agent|workflow):\d+\)/g, "@$1");

/** A run's input: the handoff's brief, a blank line, then the message's text (just the text with
 * no brief). */
const runInput = (brief: string | null, body: string) => (brief ? `${brief}\n\n${messageText(body)}` : messageText(body));

/** What a mentioned member's handoff writes: what it understood and the brief, or why it failed. */
export type Handoff = { ack: string; brief: string } | { error: string };
export const HANDOFF: Handoff = { ack: "On it.", brief: "" };
/** The acknowledgement's placeholder, until the handoff writes what was understood. */
const ACKNOWLEDGEMENT = "Reading the chat…";
const CHAT_NAME_TAKEN = "The organisation already has a chat with that name.";

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
   * The organisation `me` is in, as `GET /api/organisation` answers: their personal one, which
   * they created, with them as its first member. `me` is its creator while `creator` is them.
   */
  organisation: OrganisationRow = {
    id: ORGANISATION_ID,
    name: "tester",
    slug: ORGANISATION_SLUG,
    personal: true,
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
  otherMemberships: { id: number; name: string; slug: string; is_creator: boolean }[] = [];
  /** Whether `otherMemberships` were joined before `organisation` (listed after it). */
  joinedEarlier = false;
  /** Names other organisations have: renaming to one answers 409, as the API does. */
  organisationNamesTaken = new Set<string>();
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
  /** The organisation's settings (`/api/organisation/settings`); switching the export on sets
   * the store up (`history_ready`) only when a test says so, as the provisioning worker would. */
  settings = {
    export_run_history: false,
    history_ready: false,
    history_exported_at: null as string | null,
    embed_run_history: false,
    history_embedded_at: null as string | null,
    history_embed_error: null as string | null,
  };
  /** Times "delete exported history" was asked for. */
  exportedHistoryDeletes = 0;
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
  /** Dropped tables and columns in the organisation's store, newest first. */
  dropped: DroppedRow[] = [];
  /**
   * Notifications, oldest first. As on the server, a pending request the user may decide
   * notifies them, and deciding it marks that read; `mine: false` ones are another user's.
   */
  notifications: NotificationRow[] = [];
  /** How many times the app asked for the user's notifications. */
  notificationLists = 0;
  /** The query of each read of a chat's messages, in order (e.g. "?after=12"). */
  messageReads: string[] = [];
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
    return { ...this.organisationSummary(), ...this.organisation };
  }

  private inviteOut({ token: _token, ...invite }: InviteRow) {
    return invite;
  }

  /** The signed-in user as `/api/me` (and signing in) answers. */
  private meOut(account: Account) {
    if (!this.member) return { ...accountOut(account), organisations: this.otherMemberships };
    const { id, name, slug, is_creator } = this.organisationSummary();
    const own = { id, name, slug, is_creator };
    const organisations = this.joinedEarlier ? [own, ...this.otherMemberships] : [...this.otherMemberships, own];
    return { ...accountOut(account), organisations };
  }

  /**
   * `me` enters `organisation` (made or joined just now): the one they were in, if any, stays
   * theirs, listed after it as joined earlier.
   */
  private enter(organisation: OrganisationRow) {
    if (this.member) {
      const { id, name, slug, is_creator } = this.organisationSummary();
      const earlier = { id, name, slug, is_creator };
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
      this.organisation.members.push({ user: account, joined_at: now, invite_id: own.id });
      this.member = true;
    } else if (other) {
      this.organisation = {
        ...other.organisation,
        slug: slugify(other.organisation.name),
        personal: false,
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
    const { id, name, slug, personal, creator } = this.organisation;
    return { id, name, slug, personal, is_creator: creator?.id === this.me.id };
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

  /** The tables endpoints, as `aios.api.data` answers them. */
  private tablesResponse(path: string, params: URLSearchParams): [number, unknown] {
    if (path === "/api/data/tables") {
      return [
        200,
        {
          tables: this.dataTables.map((t) => ({
            name: t.name,
            description: t.description,
            columns: t.columns,
            row_count: t.rowCount ?? t.rows.length,
            row_count_estimated: t.estimated ?? false,
          })),
          truncated: false,
        },
      ];
    }
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
  addDocument(filename: string, fields: Partial<DocumentRow> = {}): DocumentRow {
    const doc: DocumentRow = {
      id: this.id(),
      filename,
      status: "ready",
      error: null,
      chunk_count: 1,
      size_bytes: 2048,
      uploaded_by: null,
      created_at: NOW,
      ...fields,
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

  /** Adds a tool, with no settings, to the library. */
  addTool(key: string, name: string) {
    this.tools.push({ key, name, description: `${name}.`, config_schema: {}, default_config: {} });
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
      impact: [],
      ...fields,
    };
    this.schemaChanges.push(request);
    if (request.status === "pending" && request.decider) {
      this.addNotification({ ref: { request_id: request.id, data_store_id: request.data_store_id } });
    }
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
    const { purge_in_days, refuseRestore: _, stallRestore: __, ...fields } = item;
    return { ...fields, purge_at: new Date(Date.now() + purge_in_days * 86400_000).toISOString() };
  }

  /** Adds a notification: the user's own, about a schema change request, unless told otherwise. */
  addNotification(fields: Partial<NotificationRow> = {}): NotificationRow {
    const notification: NotificationRow = {
      id: `note-${this.id()}`,
      organisation: { id: this.organisation.id, slug: this.organisation.slug },
      kind: "schema_change_request",
      ref: {},
      title: null,
      // Later ones are newer, so they list first.
      created_at: new Date(Date.parse(NOW) + this.notifications.length * 1000).toISOString(),
      read_at: null,
      mine: true,
      ...fields,
    };
    this.notifications.push(notification);
    return notification;
  }

  private notificationOut({ mine: _mine, ...n }: NotificationRow) {
    const request = n.kind === "schema_change_request"
      ? this.schemaChanges.find((r) => r.id === n.ref.request_id)
      : undefined;
    return { ...n, title: request ? request.summary : n.title };
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

  /**
   * Adds a chat run of the agent, at its current version, started by `me` unless `fields` say
   * otherwise; not tracked unless they say.
   */
  addAgentRun(
    agent: AgentRow,
    title: string | null = null,
    message_count = 0,
    fields: Partial<AgentRunCounters & Pick<AgentRunRow, "created_by" | "trigger_id">> = {},
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
      ...NOT_TRACKED,
      ...fields,
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
    this.saveVersion(workflow, next.nodes, next.edges, note);
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
   * Adds a chat, private, of `organisation` and owned by `me` unless said otherwise; the owner is
   * its first member, and made it.
   */
  addChat(
    name: string,
    {
      description = "",
      visibility = "private" as ChatVisibility,
      owner = this.me,
      organisation_id = this.organisation.id,
    }: { description?: string; visibility?: ChatVisibility; owner?: ChatUser; organisation_id?: number } = {},
  ): ChatRow {
    const chat: ChatRow = {
      id: this.id(),
      organisation_id,
      created_by: owner,
      updated_by: owner,
      name,
      description,
      visibility,
      members: [],
      messages: [],
      created_at: NOW,
      updated_at: NOW,
    };
    chat.members.push({ id: this.id(), kind: "user", role: "owner", user: owner, added_at: NOW, last_read: null });
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
  private invoke(chat: ChatRow, message: ChatMessageRow, kind: Kind, target: AgentRow | WorkflowRow): InvocationRow {
    const invocation: InvocationRow = {
      id: this.id(),
      chat_id: chat.id,
      message_id: message.id,
      requested_by: this.me.id,
      target_kind: kind,
      target_id: target.id,
      status: "queued",
      error: null,
      brief: null,
      run_id: null,
      created_at: NOW,
      started_at: null,
      finished_at: null,
    };
    this.invocations.set(invocation.id, invocation);
    this.addChatMessage(
      chat,
      ACKNOWLEDGEMENT,
      { kind, id: target.id, name: target.name },
      [],
      { kind: "run_card", invocation_id: invocation.id },
    );
    // An archived agent's or workflow's fails at once, as the API's does: no handoff, no run.
    if (target.archived_at !== null) {
      const error = `The ${kind} ${target.name} is archived`;
      this.finishInvocation(invocation, "failed", `Couldn't start the run: ${error}`, error);
    }
    return invocation;
  }

  /** Ends an invocation and posts its result back from its agent or workflow, as the API does. */
  private finishInvocation(invocation: InvocationRow, status: "succeeded" | "failed", body: string, error: string | null = null) {
    Object.assign(invocation, { status, error, finished_at: NOW });
    const target = this.targetOf(invocation);
    this.addChatMessage(
      this.chats.get(invocation.chat_id)!,
      body,
      { kind: invocation.target_kind, id: target.id, name: target.name },
      [],
      { invocation_id: invocation.id },
    );
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
      if (invocation.chat_id !== chat.id) continue;
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
      // Titled after what was asked, not the brief.
      const run = this.addAgentRun(agent, messageText(message.body), 1, {
        status: "running",
        started_at: new Date().toISOString(),
        tool_call_count: 0,
        input_tokens: 0,
        output_tokens: 0,
      });
      run.ui_messages = [{ id: "u1", role: "user", parts: [{ type: "text", text: input }] }];
      Object.assign(invocation, { status: "running", run_id: run.id, started_at: run.started_at });
    } else if (invocation.status === "running") {
      const run = this.agentRuns.get(invocation.run_id!);
      if (!run) {
        const deleted = "The run was deleted before it finished.";
        this.finishInvocation(invocation, "failed", `The run failed: ${deleted}`, deleted);
        return;
      }
      if (run.tool_call_count! < turn.steps) {
        run.tool_call_count! += 1;
        run.input_tokens! += turn.tokensPerStep - 20;
        run.output_tokens! += 20;
        return;
      }
      const finished_at = new Date().toISOString();
      if (turn.error) {
        Object.assign(run, { status: "failed", finished_at });
        this.finishInvocation(invocation, "failed", `The run failed: ${turn.error}`, turn.error);
        return;
      }
      const reply = turn.reply ?? `Done: ${run.title}`;
      Object.assign(run, { status: "succeeded", finished_at, message_count: 2 });
      run.ui_messages = [...run.ui_messages, { id: "a1", role: "assistant", parts: [{ type: "text", text: reply }] }];
      this.finishInvocation(invocation, "succeeded", reply);
    }
  }

  /** Adds a user to a chat, as the owner would. */
  addChatMember(chat: ChatRow, user: ChatUser): ChatUserMemberRow {
    const member: ChatUserMemberRow = { id: this.id(), kind: "user", role: "member", user, added_at: NOW, last_read: null };
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

  /** Adds an agent or a workflow to a chat, as the owner would. */
  addChatTarget(chat: ChatRow, kind: Kind, target: { id: number }): ChatMemberRow {
    const member: ChatMemberRow = { id: this.id(), kind, target_id: target.id, added_at: NOW };
    chat.members.push(member);
    return member;
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
      created_by: agent.created_by,
      updated_by: agent.updated_by,
      archived_at: agent.archived_at,
      archived_by: agent.archived_by,
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
    };
  }

  private chatSummary(chat: ChatRow) {
    return {
      id: chat.id,
      name: chat.name,
      description: chat.description,
      visibility: chat.visibility,
      is_owner: this.ownsChat(chat),
      is_member: this.inChat(chat),
      member_count: this.liveMembers(chat).length,
      created_by: chat.created_by,
      updated_by: chat.updated_by,
      created_at: chat.created_at,
      updated_at: chat.updated_at,
    };
  }

  /** The agent or workflow a member is. */
  private chatTarget(kind: Kind, id: number) {
    return kind === "agent" ? this.agents.get(id) : this.workflows.get(id);
  }

  /** Whether `me` holds the chat's owner role, on their member row. */
  private ownsChat(chat: ChatRow): boolean {
    return chat.members.some((m) => m.kind === "user" && m.role === "owner" && m.user.id === this.me.id);
  }

  /** The chat's members as the API shows them: owner first. */
  private liveMembers(chat: ChatRow): ChatMemberOut[] {
    return chat.members
      .flatMap((m): ChatMemberOut[] => {
        const { id, kind, added_at } = m;
        if (kind === "user") {
          const { user, role } = m;
          return [{ id, kind, role, target_id: user.id, name: fullName(user), added_at, archived: false }];
        }
        const target = this.chatTarget(kind, m.target_id);
        if (!target) return [];
        const archived = target.archived_at !== null;
        return [{ id, kind, role: null, target_id: target.id, name: target.name, added_at, archived }];
      })
      .sort((a, b) => Number(a.role !== "owner") - Number(b.role !== "owner") || a.id - b.id);
  }

  chatOut(chat: ChatRow) {
    return { ...this.chatSummary(chat), members: byKind(this.liveMembers(chat)) };
  }

  /** Who can be mentioned in a chat, as `GET /api/chats/{id}/mentionable` groups them: not its
   * archived agents and workflows, unless `archived` (posting takes a mention of one, which fails
   * at once). */
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
    const rest = { ...invocationRest, acknowledgement: this.cardOf(chat, invocation).body };
    if (invocation.target_kind === "agent") {
      const run = run_id === null ? undefined : this.agentRuns.get(run_id);
      return {
        ...rest,
        workflow_run_id: null,
        agent_run_id: run ? run.id : null,
        run_deleted: invocation.started_at !== null && !run,
        workflow_run: null,
        agent_run: run
          ? {
              id: run.id,
              status: run.status,
              started_at: run.started_at,
              finished_at: run.finished_at,
              tool_call_count: run.tool_call_count,
              input_tokens: run.input_tokens,
              output_tokens: run.output_tokens,
              waiting: this.schemaChanges.some(
                (r) => r.run_id === run.id && !r.resumed_at && r.status !== "cancelled",
              ),
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
    };
  }

  /** Messages as the API returns them, with the invocations they belong to and those still going. */
  private messagesOut(chat: ChatRow, messages: ChatMessageRow[]) {
    const ids = new Set(messages.map((m) => m.invocation_id));
    const invocations = [...this.invocations.values()].filter(
      (i) => i.chat_id === chat.id && (ids.has(i.id) || (i.status !== "succeeded" && i.status !== "failed")),
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
      const pinned = this.workflowVersions.get(n.version_id)!;
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
      const own = this.invites.find((i) => i.token === token);
      const invitation =
        this.invitations.get(token) ??
        (own && {
          organisation: { id: this.organisation.id, name: this.organisation.name },
          expires_at: own.expires_at,
        });
      if (!invitation) return fail(404, "This invitation link has expired or been revoked, or never existed.");
      if (!m[2] && method === "GET") return reply(200, invitation);
      if (this.signedInAs === null) return fail(401, "Not signed in");
      // As the API: a membership is added, whatever else `me` is in; one they have already is
      // answered alike, and nothing changes.
      if (invitation.organisation.id === this.organisation.id) {
        if (!this.member) {
          this.organisation.members.push({ user: this.me, joined_at: new Date().toISOString(), invite_id: own?.id ?? null });
          this.member = true;
        }
        return reply(200, this.organisationOut());
      }
      const already = this.otherMemberships.find((o) => o.id === invitation.organisation.id);
      if (already) return reply(200, { ...already, personal: false, creator: null, members: [], created_at: "2026-10-01T08:00:00Z" });
      this.enter({
        ...invitation.organisation,
        slug: slugify(invitation.organisation.name),
        personal: false,
        creator: null,
        members: [{ user: this.me, joined_at: new Date().toISOString(), invite_id: own?.id ?? null }],
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
    if (path === "/api/organisations" && method === "POST") {
      const name = String(body?.name ?? "").trim();
      if (!name) return reply(422, { detail: [{ loc: ["body", "name"], msg: "String should match pattern '\\S'" }] });
      if (this.organisationNamesTaken.has(name)) return fail(409, "An organisation with that name already exists.");
      const now = new Date().toISOString();
      this.enter({
        id: this.id(),
        name,
        slug: slugify(name),
        personal: false,
        creator: this.me,
        members: [{ user: this.me, joined_at: now, invite_id: null }],
        created_at: now,
      });
      this.invites = [];
      return reply(201, this.organisationOut());
    }
    // Everything but the person's own (signing in, the profile, invitations, notifications, the
    // tool library and timezones) is an organisation's, under its prefix (`/api/o/<id>/...`), as
    // in the API: the fake's organisation answers (as it was before the prefix, from here on),
    // and any other is a 404, as for one that doesn't exist. Nothing answers without the prefix.
    const unscoped = /^\/api\/(notifications|tools|triggers\/timezones)(\/|$)/.test(path);
    if (!unscoped) {
      const scoped = path.match(/^\/api\/o\/([^/]+)(\/.*)$/);
      if (!scoped) return fail(404, "Not Found");
      if (!this.member || scoped[1] !== String(this.organisation.id)) return fail(404, "Organisation not found");
      path = `/api${scoped[2]}`;
    }
    if (path === "/api/organisation/invites" || (m = path.match(/^\/api\/organisation\/invites\/([\w-]+)$/))) {
      if (this.organisation.creator?.id !== this.me.id) {
        return fail(403, "Only the organisation's creator can invite people into it.");
      }
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
      if (this.organisation.creator?.id === this.me.id) {
        return fail(409, "You created this organisation, and only its creator can invite people into it, so you can't leave while others are still members.");
      }
      // Only the membership goes: `me` is left in the others they're in, or in none.
      this.organisation.members = this.organisation.members.filter((m) => m.user.id !== this.me.id);
      this.member = false;
      return reply(200, this.meOut(account));
    }
    if (path === "/api/organisation") {
      if (method === "PUT") {
        if (this.organisation.creator?.id !== this.me.id) {
          return fail(403, "Only the organisation's creator can rename it.");
        }
        const name = String(body?.name ?? "").trim();
        if (!name) return fail(422, "Invalid name");
        if (this.organisationNamesTaken.has(name)) {
          return fail(409, "An organisation with that name already exists.");
        }
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
    if (path === "/api/organisation/settings") {
      if (method === "PUT") {
        // As the API: embedding only while the export is on, and off with it.
        if (body?.export_run_history !== undefined) {
          this.settings.export_run_history = Boolean(body.export_run_history);
          if (!this.settings.export_run_history) this.settings.embed_run_history = false;
        }
        if (body?.embed_run_history !== undefined) {
          if (body.embed_run_history && !this.settings.export_run_history) {
            return reply(409, { detail: "Switch the run history export on first." });
          }
          if (body.embed_run_history && !this.settings.embed_run_history) {
            this.settings.history_embedded_at = null;
          }
          this.settings.embed_run_history = Boolean(body.embed_run_history);
        }
      }
      return reply(200, this.settings);
    }
    if (path === "/api/organisation/settings/exported-history" && method === "DELETE") {
      this.exportedHistoryDeletes++;
      // Emptied: nothing exported since (while it's on, the export starts over).
      this.settings.history_exported_at = null;
      return reply(204);
    }
    if (
      path === "/api/documents" ||
      path.startsWith("/api/documents/") ||
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
        const [status, json] = this.tablesResponse(path, new URL(request.url()).searchParams);
        return reply(status, json);
      }
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
    if ((m = path.match(/^\/api\/schema-changes\/([\w-]+)$/)) && method === "GET") {
      const request = this.schemaChanges.find((r) => r.id === m![1]);
      if (!request || !request.decider) return fail(404, "Schema change request not found");
      return reply(200, this.schemaChangeOut(request));
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
      for (const n of this.notifications) {
        if (n.ref.request_id === request.id && !n.read_at) n.read_at = NOW;
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
      else if (!item.stallRestore) this.dropped = this.dropped.filter((d) => d !== item);
      return reply(202, { ...out, blocked: null });
    }
    if (path === "/api/notifications" && method === "GET") {
      this.notificationLists++;
      const mine = this.notifications.filter((n) => n.mine).reverse();
      const unread = mine.filter((n) => !n.read_at);
      return reply(200, {
        unread: unread.length,
        notifications: [...unread, ...mine.filter((n) => n.read_at)].map((n) =>
          this.notificationOut(n),
        ),
      });
    }
    if ((m = path.match(/^\/api\/notifications\/([\w-]+)\/read$/)) && method === "POST") {
      const notification = this.notifications.find((n) => n.id === m![1] && n.mine);
      if (!notification) return fail(404, "Notification not found");
      notification.read_at ??= NOW;
      return reply(200, this.notificationOut(notification));
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
      return reply(200, { workflows, triggers, chats, tables: [] });
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
      this.saveVersion(workflow, body.nodes, body.edges, body.note ?? null);
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
        workflow.name = body.name;
        workflow.description = body.description ?? "";
        workflow.updated_by = this.me;
        this.applyLayout(workflow, body);
        this.saveVersion(workflow, body.nodes, body.edges, body.note ?? null);
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
              (n) => n.kind === "workflow" && this.workflowVersions.get(n.version_id)!.workflow_id === workflow.id,
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
      if ((m = rest.match(/^\/runs\/(\d+)\/subworkflow$/)) && method === "GET") {
        const run = this.runs.get(Number(m[1]));
        if (!run || run.workflow_id !== workflow.id) return fail(404, "Not Found");
        // The version pinned at the path's workflow node, along the run's graph.
        let version = this.workflowVersions.get(run.workflow_version_id)!;
        for (const key of url.searchParams.getAll("path")) {
          const node = version.nodes.find((n) => n.key === key);
          if (!node || node.kind !== "workflow") return fail(404, "Not Found");
          version = this.workflowVersions.get(node.version_id)!;
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
        this.saveVersion(workflow, old.nodes, old.edges, `Restored from v${old.version}`);
        return reply(200, this.workflowOut(workflow));
      }
    }
    if (path === "/api/chats" && method === "GET") {
      // Most recently active first; everything here happens at NOW, so the newest first.
      return reply(
        200,
        this.organisationChats()
          // Mine, and the public ones I'm not in, for me to find.
          .filter((c) => this.inChat(c) || c.visibility === "public")
          .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id)
          .map((c) => ({ ...this.chatSummary(c), unread_count: this.unreadCount(c) })),
      );
    }
    if (path === "/api/chats" && method === "POST") {
      if (this.organisationChats().some((c) => c.name === body.name)) {
        return fail(409, CHAT_NAME_TAKEN);
      }
      const chat = this.addChat(body.name, {
        description: body.description ?? "",
        visibility: body.visibility ?? "private",
      });
      // A new chat's first agent is the organisation's documents assistant.
      this.addChatTarget(chat, "agent", this.documentsAssistant());
      return reply(201, this.chatOut(chat));
    }
    if ((m = path.match(/^\/api\/chats\/(\d+)(\/.*)?$/))) {
      const chat = this.chats.get(Number(m[1]));
      const rest = m[2] ?? "";
      // Another organisation's chat doesn't exist here; nor a private one for a non-member.
      if (
        !chat ||
        chat.organisation_id !== this.organisation.id ||
        (chat.visibility === "private" && !this.inChat(chat))
      ) {
        return fail(404, "Not Found");
      }
      if (rest === "" && method === "GET") return reply(200, this.chatOut(chat));
      if (rest === "/messages" && method === "GET") {
        this.messageReads.push(url.search);
        this.advanceInvocations(chat);
        return reply(200, this.messagesPage(chat, url.searchParams));
      }
      if (rest === "/mentionable" && method === "GET") return reply(200, this.mentionable(chat));
      if (rest === "/join" && method === "POST") {
        if (this.inChat(chat)) return fail(409, "Already a member of this chat.");
        // What was there to read before they joined counts as seen.
        this.addChatMember(chat, this.me).last_read = chat.messages.at(-1)?.id ?? null;
        return reply(200, this.chatOut(chat));
      }
      if (rest === "/leave" && method === "POST") {
        const mine = this.myMembership(chat);
        if (!mine) return fail(409, "Not a member of this chat.");
        if (mine.role === "owner") return fail(422, "The owner can't leave the chat.");
        chat.members = chat.members.filter((x) => x !== mine);
        return reply(204);
      }
      if (rest === "/read" && method === "POST") {
        const mine = this.myMembership(chat);
        if (!mine) return fail(403, "Only the chat's members keep track of what they've read in it.");
        this.chatReads.push([chat.id, body.message_id]);
        const seen = Math.min(body.message_id, chat.messages.at(-1)?.id ?? 0);
        mine.last_read = Math.max(mine.last_read ?? 0, seen);
        return reply(204);
      }
      if (rest === "/messages" && method === "POST") {
        if (!this.inChat(chat)) return fail(403, "Only the chat's members can post in it.");
        const members = Object.values(this.mentionable(chat, true)).flat();
        const mentions: ChatMention[] = [];
        for (const { kind, id } of (body.mentions ?? []) as ChatMention[]) {
          const member = members.find((x) => x.kind === kind && x.id === id);
          if (!member) return fail(422, "Only the chat's members can be mentioned in it.");
          const twice = mentions.some((x) => x.kind === kind && x.id === id);
          if (!twice) mentions.push({ kind, id, name: member.name });
        }
        const message = this.addChatMessage(chat, body.body, this.me, mentions);
        const invoked = mentions.flatMap((x) =>
          x.kind === "user" ? [] : [this.invoke(chat, message, x.kind, (x.kind === "agent" ? this.agents : this.workflows).get(x.id)!)],
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
      if (!this.ownsChat(chat)) return fail(403, "Only the chat's owner can do that.");
      if (rest === "" && method === "PUT") {
        if (this.organisationChats().some((c) => c.name === body.name && c !== chat)) {
          return fail(409, CHAT_NAME_TAKEN);
        }
        Object.assign(chat, {
          updated_by: this.me,
          name: body.name,
          description: body.description ?? "",
          visibility: body.visibility ?? "private",
        });
        return reply(200, this.chatOut(chat));
      }
      if (rest === "" && method === "DELETE") {
        this.chats.delete(chat.id);
        // Its messages and invocations go with it; the runs stay.
        for (const i of this.invocations.values()) if (i.chat_id === chat.id) this.invocations.delete(i.id);
        return reply(204);
      }
      if (rest === "/members" && method === "POST") {
        const kind = body.kind as ChatMention["kind"];
        if (kind === "user") {
          // Only a member of the organisation: anyone else doesn't exist here.
          const found = this.organisation.members.find((x) => x.user.id === body.id);
          if (!found) return fail(404, "Unknown user");
          if (chat.members.some((x) => x.kind === "user" && x.user.id === body.id)) {
            return fail(409, "Already a member of this chat.");
          }
          this.addChatMember(chat, found.user);
          return reply(201, this.chatOut(chat));
        }
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
        if (member.kind === "user" && member.role === "owner") {
          return fail(422, "The owner can't be removed from the chat.");
        }
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
