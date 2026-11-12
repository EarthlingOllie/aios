import { execSync } from "node:child_process";

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * The core loop against the real stack (Compose's `fullstack` profile), every turn on the run
 * service and every store reached through the pooler: a person mentions an agent in a chat; the
 * agent, on the scripted model's `core-loop` scenario (backend/src/aios/testing/scenarios.py),
 * asks for a `notes` table and its card shows the run waiting; the person approves on the Data
 * page, which takes an editor; the urgent worker applies the change, the run service resumes the
 * run, which writes a row and replies "Saved." in the chat; each of its model calls wrote a row of
 * the usage ledger; and the audit log recorded the decision.
 *
 * A second story restarts the run service mid-turn (a deploy): the turn is handed back to the
 * queue and carries on in the new process, and the chat gets its reply, never "The service
 * stopped mid-run."
 *
 * A third story runs a workflow that branches on an agent's structured output, pauses at an
 * approval node for an admin, and resumes on the decision made in the Data page's Approvals list.
 *
 * A fourth runs a table written trigger: once the core loop's notes table is shared with an agent
 * that appends to it, a trigger on the table runs that agent once when the core loop notes
 * something again; the agent's own note is its echo, which the dispatcher's loop rules refuse.
 *
 * A fifth mentions an agent twice in one chat, an editor changing it in between: the second
 * mention is another turn of the same run, on the new version, and the run's page shows each
 * turn's version.
 *
 * Later steps add their stories here, each a scenario of the scripted model.
 */

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const cardStatus = (page: Page) => messageList(page).getByTestId("run-card-status");

const NOTES_TOOLS = [
  { key: "request_schema_change", config: {} },
  { key: "write_data", config: {} },
];

async function created<T>(request: APIRequestContext, url: string, data: object): Promise<T> {
  const response = await request.post(url, { data });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()) as T;
}

/** Registers a new person, who creates an organisation (its store provisioned by the routine
 * worker), an agent that may ask for tables and write to them, and a chat it's in. */
async function setUp(page: Page) {
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;

  // Register, which makes nothing, then create an organisation on the welcome page: its store is
  // provisioned by the routine worker.
  await page.goto("/register");
  await page.getByLabel("First name").fill("Core");
  await page.getByLabel("Last name").fill("Loop");
  await page.getByLabel("Email").fill(`core-loop-${suffix}@example.com`);
  await page.getByLabel("Password", { exact: true }).fill("a long passphrase");
  await page.getByLabel("Confirm password").fill("a long passphrase");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/welcome$/);
  await expect(page.getByRole("heading", { name: "Welcome to aios" })).toBeVisible();
  await page.getByLabel("Organisation name").fill(`Core Loop ${suffix}`);
  await page.getByRole("button", { name: "Create organisation" }).click();
  await expect(page).toHaveURL(/\/o\/[^/]+\//);
  // Their organisation: the API names it by its id (`/api/o/<id>/...`), links by its slug.
  const me = await page.request.get("/api/me");
  expect(me.status(), await me.text()).toBe(200);
  const {
    organisations: [organisation],
  } = (await me.json()) as { organisations: { id: number; slug: string }[] };
  const api = (path: string) => `/api/o/${organisation.id}${path}`;
  const link = (path: string) => `/o/${organisation.slug}${path}`;
  await expect(page).toHaveURL(new RegExp(`^[^?#]*${link("")}(/|$)`));
  await expect
    .poll(
      async () => {
        const response = await page.request.get(api("/data-store"));
        return response.ok() ? (await response.json()).status : `HTTP ${response.status()}`;
      },
      { message: "the organisation's data store is provisioned", timeout: 60_000 },
    )
    .toBe("ready");

  // An agent that may ask for tables and write to them, in a chat (their screens have tests of
  // their own).
  const agent = await created<{ id: number }>(page.request, api("/agents"), { name: "Notes", tools: NOTES_TOOLS });
  const chat = await created<{ id: number }>(page.request, api("/chats"), { name: `Notes ${suffix}` });
  await created(page.request, api(`/chats/${chat.id}/members`), { kind: "agent", id: agent.id });
  return { api, link, agent, chat };
}

/** Mentions the agent (the Notes agent unless `name` says) in the chat, with `text` after the
 * mention. */
async function mention(page: Page, text: string, name = "Notes") {
  await composer(page).pressSequentially(`@${name.slice(0, 3)}`);
  await page.getByRole("listbox", { name: "Mention" }).getByRole("option", { name: `${name} agent` }).click();
  await composer(page).pressSequentially(` ${text}`);
  await composer(page).press("Enter");
}

test("a mention asks for a table, waits for approval on the Data page, then saves and replies", async ({
  page,
}) => {
  const { api, link, agent, chat } = await setUp(page);

  // Mention the agent.
  await page.goto(link(`/chats/${chat.id}`));
  await mention(page, "buy milk");

  // It asks for a notes table, and its card shows the run waiting.
  await expect(cardStatus(page)).toHaveText("Waiting for you", { timeout: 60_000 });

  // Approve on the Data page.
  await page.getByRole("link", { name: "Data" }).first().click();
  await page.getByRole("tab", { name: /Approvals/ }).click();
  const request = page
    .getByRole("region", { name: "Pending approvals" })
    .getByRole("region", { name: "Schema change: Create table data.notes" });
  await expect(request).toContainText("To keep the chat's notes");
  await request.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("region", { name: "Approval history" })).toContainText("Create table data.notes");
  await expect(page).toHaveURL(new RegExp(`^[^?#]*${link("/data/approvals")}`));

  // The run resumes, writes its row and replies in the chat.
  await page.goto(link(`/chats/${chat.id}`));
  const reply = messageList(page).getByRole("article").filter({ hasText: "Saved." });
  await expect(reply).toBeVisible({ timeout: 60_000 });
  await expect(reply.getByText("Notes", { exact: true })).toBeVisible();
  await expect(cardStatus(page)).toHaveText("Succeeded");

  // The row it wrote, on the Data page's Tables.
  await page.goto(link("/data/tables?table=notes"));
  await expect(page.getByRole("table", { name: "Rows of notes" }).getByRole("row")).toHaveText([
    /id\s*body/,
    /1\s*@Notes\s+buy milk/,
  ]);

  // Every model call of its turns wrote a usage row, charged to the agent, and so did the
  // handoff's one call.
  const runs = (await (await page.request.get(api(`/agents/${agent.id}/runs`))).json()) as { id: number }[];
  let modelCalls = 0;
  for (const run of runs) {
    const detail = (await (await page.request.get(api(`/agents/${agent.id}/runs/${run.id}`))).json()) as {
      turns: { model_calls: number }[];
    };
    modelCalls += detail.turns.reduce((sum, t) => sum + t.model_calls, 0);
  }
  expect(modelCalls).toBeGreaterThan(0);
  const usage = await page.request.get(api("/usage?group=agent"));
  expect(usage.status(), await usage.text()).toBe(200);
  const totals = (await usage.json()) as { currency: string; totals: { key: number; model_calls: number }[] };
  expect(totals.currency).toBe("GBP");
  expect(totals.totals.find((t) => t.key === agent.id)?.model_calls).toBe(modelCalls + 1);

  // The approval took an editor (creating tables does, by default), and the audit log recorded
  // who decided it.
  const audit = await page.request.get(api("/audit-log?action=approval"));
  expect(audit.status(), await audit.text()).toBe(200);
  const { entries } = (await audit.json()) as {
    entries: { action: string; actor: { user: { name: string } | null }; details: Record<string, string> }[];
  };
  const decided = entries.filter((e) => e.action === "approval.decided");
  expect(decided).toHaveLength(1);
  expect(decided[0].details).toMatchObject({
    decision: "approved",
    summary: "Create table data.notes",
    needs_role: "editor",
  });
  expect(decided[0].actor.user?.name).toBe("Core Loop");
});

/** Restarts the run service as a deploy would: a termination signal, its grace, then a new
 * process. Compose's by default; FULLSTACK_RESTART_RUN_SERVICE names another command (a stack
 * run another way). */
function restartRunService() {
  const command =
    process.env.FULLSTACK_RESTART_RUN_SERVICE ||
    "docker compose -f ../docker-compose.yml --profile fullstack restart fullstack-run-service";
  execSync(command, { stdio: "inherit", timeout: 90_000 });
}

test("restarting the run service mid-turn hands the turn back, and it carries on to its reply", async ({
  page,
}) => {
  const { link, chat } = await setUp(page);
  await page.goto(link(`/chats/${chat.id}`));

  // The scripted model takes its time over a message asking it to (longer than the run
  // service's shutdown grace in the stack), and then replies.
  await mention(page, "write this slowly");
  await expect(cardStatus(page)).toHaveText("Running", { timeout: 60_000 });
  restartRunService();

  const reply = messageList(page).getByRole("article").filter({ hasText: "Written, slowly." });
  await expect(reply).toBeVisible({ timeout: 90_000 });
  await expect(cardStatus(page)).toHaveText("Succeeded");
  await expect(messageList(page)).not.toContainText("The service stopped mid-run.");
  await expect(messageList(page)).not.toContainText("The turn was stopped.");
});

test("a workflow branches on structured output, pauses at an approval node, and resumes on the decision", async ({
  page,
}) => {
  const { api, link } = await setUp(page);
  // On the scripted model: `classify` gives {risk: "high", amount: 250} as structured output; the
  // others reply as they're told (backend/src/aios/testing/scenarios.py).
  const agent = (name: string, base_prompt: string) =>
    created<{ current_version: { id: number } }>(page.request, api("/agents"), { name, base_prompt });
  const classify = await agent("Classify", "Classify the spend.");
  const pay = await agent("Pay", "Reply with: Paid.");
  const tell = await agent("Tell", "Reply with: Not approved.");
  const auto = await agent("Auto", "Reply with: No approval needed.");
  const high = { "==": [{ var: "output.risk" }, "high"] };
  const workflow = await created<{ id: number }>(page.request, api("/workflows"), {
    name: "Spend",
    nodes: [
      {
        key: "classify",
        kind: "agent",
        version_id: classify.current_version.id,
        output_schema: {
          type: "object",
          properties: { risk: { type: "string" }, amount: { type: "number" } },
          required: ["risk", "amount"],
        },
      },
      {
        key: "approve",
        kind: "approval",
        prompt_template: "Spend {{nodes.classify.amount}} on {{input}}?\nThe risk is {{nodes.classify.risk}}.",
        approver_role: "admin",
      },
      { key: "pay", kind: "agent", version_id: pay.current_version.id },
      { key: "tell", kind: "agent", version_id: tell.current_version.id },
      { key: "auto", kind: "agent", version_id: auto.current_version.id },
    ],
    edges: [
      { source: "classify", target: "approve", condition: high },
      { source: "classify", target: "auto", condition: { "!": [high] } },
      { source: "approve", target: "pay", condition: { "==": [{ var: "output.approved" }, true] } },
      { source: "approve", target: "tell", condition: { "!": [{ var: "output.approved" }] } },
    ],
  });
  const run = await created<{ id: number }>(page.request, api(`/workflows/${workflow.id}/runs`), {
    input: "laptops",
  });

  // It branches on the structured output to the approval node, and waits there.
  await page.goto(link(`/workflows/${workflow.id}/runs/${run.id}`));
  await expect(page.getByTestId("run-status")).toHaveText("Waiting for you", { timeout: 60_000 });
  const waiting = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(waiting).toContainText("approve");

  // Decide it in the Approvals list, where its approver sees the prompt and input in full.
  await page.goto(link("/data/approvals?kind=workflow_step"));
  const approval = page
    .getByRole("region", { name: "Pending approvals" })
    .getByRole("region", { name: "Approval: Spend 250 on laptops?" });
  await expect(approval.getByLabel("Prompt")).toHaveText("Spend 250 on laptops?\nThe risk is high.");
  await expect(approval.getByLabel("Input")).toContainText('"amount": 250');
  await approval.getByRole("button", { name: "Approve" }).click();
  await expect(
    page.getByRole("region", { name: "Approval history" }).getByRole("region", { name: "Approval: Spend 250 on laptops?" }),
  ).toContainText("Approved");

  // The run resumes from the decision: the approved branch runs, the other is skipped.
  await page.goto(link(`/workflows/${workflow.id}/runs/${run.id}`));
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded", { timeout: 60_000 });
  const detail = (await (await page.request.get(api(`/workflows/${workflow.id}/runs/${run.id}`))).json()) as {
    output: string;
    steps: { path: string[]; status: string; output_json: Record<string, unknown> | null }[];
  };
  const step = (key: string) => detail.steps.find((s) => s.path.join("/") === key);
  expect(step("auto")?.status).toBe("skipped");
  expect(step("approve")?.output_json).toMatchObject({ approved: true, status: "approved", decided_by: { name: "Core Loop" } });
  expect(step("pay")?.status).toBe("succeeded");
  expect(step("tell")?.status).toBe("skipped");
  expect(detail.output).toBe("Paid.");

  // The decision is in the audit log.
  const audit = await page.request.get(api("/audit-log?action=approval"));
  const { entries } = (await audit.json()) as { entries: { action: string; details: Record<string, string> }[] };
  expect(entries.find((e) => e.action === "approval.decided")?.details).toMatchObject({
    kind: "workflow_step",
    decision: "approved",
    needs_role: "admin",
  });
});

test("a table written trigger fires once, and its echo is refused", async ({ page }) => {
  const { api, link, chat } = await setUp(page);
  await page.goto(link(`/chats/${chat.id}`));

  // The core loop asks for its notes table; approved, it writes its first note.
  await mention(page, "buy milk");
  const pending = async () => (await (await page.request.get(api("/approvals"))).json()) as { id: number }[];
  await expect.poll(async () => (await pending()).length, { timeout: 60_000 }).toBe(1);
  const [approval] = await pending();
  const approved = await page.request.post(api(`/approvals/${approval.id}/approve`));
  expect(approved.status(), await approved.text()).toBe(200);
  await expect(messageList(page).getByRole("article").filter({ hasText: "Saved." })).toBeVisible({ timeout: 60_000 });

  // An agent that appends to notes (`APPEND` in the scenario), sharing the table, and a trigger
  // on the table that runs it.
  const appender = await created<{ id: number }>(page.request, api("/agents"), {
    name: "Appender",
    base_prompt: "Append to notes.",
    tools: NOTES_TOOLS,
  });
  await created(page.request, api("/data/tables/notes/shares"), { agent_id: appender.id });
  await expect
    .poll(
      async () => {
        const { tables } = (await (await page.request.get(api("/data/tables"))).json()) as {
          tables: { name: string; pending: boolean }[];
        };
        return tables.find((t) => t.name === "notes")?.pending;
      },
      { message: "the share is applied", timeout: 60_000 },
    )
    .toBe(false);
  const trigger = await created<{ id: number }>(page.request, api("/triggers"), {
    kind: "event",
    event_type: "table_written",
    target_kind: "agent",
    target_id: appender.id,
    filter: { tables: ["notes"] },
  });

  // The core loop notes something again: the trigger runs the appender once, whose own note is
  // refused as its echo.
  await mention(page, "note again: buy eggs");
  const trig = async () =>
    (await (await page.request.get(api(`/triggers/${trigger.id}`))).json()) as {
      last_status: string | null;
      last_error: string | null;
    };
  await expect.poll(async () => (await trig()).last_status, { timeout: 90_000 }).toBe("refused");
  expect((await trig()).last_error).toContain("own target");
  const runs = (await (await page.request.get(api(`/agents/${appender.id}/runs`))).json()) as unknown[];
  expect(runs).toHaveLength(1);

  await page.goto(link("/data/tables?table=notes"));
  await expect(page.getByRole("table", { name: "Rows of notes" }).getByRole("row")).toHaveText([
    /id\s*body/,
    /1\s*@Notes\s+buy milk/,
    /2\s*@Notes\s+note again: buy eggs/,
    /3\s*Appended by the trigger\./,
  ]);
});

test("a second mention continues the same run, on the version an editor saved in between", async ({ page }) => {
  const { api, link, chat } = await setUp(page);
  // On the scripted model, an agent told "Reply with: <text>" replies <text>.
  const echo = await created<{ id: number }>(page.request, api("/agents"), {
    name: "Echo",
    base_prompt: "Reply with: First.",
  });
  await created(page.request, api(`/chats/${chat.id}/members`), { kind: "agent", id: echo.id });
  await page.goto(link(`/chats/${chat.id}`));
  const cards = messageList(page).getByTestId("run-card");
  const reply = (text: string) => messageList(page).getByRole("article").filter({ hasText: text });

  await mention(page, "say something", "Echo");
  await expect(reply("First.")).toBeVisible({ timeout: 60_000 });

  // An editor saves a fix; the next mention in the chat runs on it, in the same run.
  const saved = await page.request.put(api(`/agents/${echo.id}`), {
    data: { name: "Echo", base_prompt: "Reply with: Second." },
  });
  expect(saved.status(), await saved.text()).toBe(200);
  await mention(page, "and again", "Echo");
  await expect(reply("Second.")).toBeVisible({ timeout: 60_000 });
  await expect(cards).toHaveCount(2);
  const name = await cards.first().getAttribute("aria-label");
  expect(name).toMatch(/^Open run #\d+$/);
  await expect(cards.nth(1)).toHaveAttribute("aria-label", name!);
  const runs = (await (await page.request.get(api(`/agents/${echo.id}/runs`))).json()) as unknown[];
  expect(runs).toHaveLength(1);

  // Its page lists both turns, each on the version it ran on.
  await cards.nth(1).click();
  await page.getByRole("tab", { name: "Turns" }).click();
  const turns = page.getByRole("listitem").filter({ hasText: /^Turn / });
  await expect(turns).toHaveCount(2);
  await expect(turns.nth(0)).toContainText("v1");
  await expect(turns.nth(1)).toContainText("v2");
});
