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

/** Mentions the agent in the chat, with `text` after the mention. */
async function mention(page: Page, text: string) {
  await composer(page).pressSequentially("@Not");
  await page.getByRole("listbox", { name: "Mention" }).getByRole("option", { name: "Notes agent" }).click();
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
  await page.getByRole("tab", { name: /Changes/ }).click();
  const request = page
    .getByRole("region", { name: "Pending changes" })
    .getByRole("region", { name: "Schema change: Create table data.notes" });
  await expect(request).toContainText("To keep the chat's notes");
  await request.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("region", { name: "Change history" })).toContainText("Create table data.notes");
  await expect(page).toHaveURL(new RegExp(`^[^?#]*${link("/data/changes")}`));

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
