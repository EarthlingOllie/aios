import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * The core loop against the real stack (Compose's `fullstack` profile): a person mentions an
 * agent in a chat; the agent, on the scripted model's `core-loop` scenario
 * (backend/src/aios/testing/scenarios.py), asks for a `notes` table and its card shows the run
 * waiting; the person approves on the Data page; the provisioning worker applies the change, the
 * run service resumes the run, which writes a row and replies "Saved." in the chat; and each
 * of its model calls wrote a row of the usage ledger.
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

test("a mention asks for a table, waits for approval on the Data page, then saves and replies", async ({
  page,
}) => {
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;

  // 1. Register, which makes nothing, then create an organisation on the welcome page: its store
  // is provisioned by the provisioning worker.
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

  // 2. Mention the agent.
  await page.goto(link(`/chats/${chat.id}`));
  await composer(page).pressSequentially("@Not");
  await page.getByRole("listbox", { name: "Mention" }).getByRole("option", { name: "Notes agent" }).click();
  await composer(page).pressSequentially(" buy milk");
  await composer(page).press("Enter");

  // 3. It asks for a notes table, and its card shows the run waiting.
  await expect(cardStatus(page)).toHaveText("Waiting for you", { timeout: 60_000 });

  // 4. Approve on the Data page.
  await page.getByRole("link", { name: "Data" }).first().click();
  await page.getByRole("tab", { name: /Changes/ }).click();
  const request = page
    .getByRole("region", { name: "Pending changes" })
    .getByRole("region", { name: "Schema change: Create table data.notes" });
  await expect(request).toContainText("To keep the chat's notes");
  await request.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("region", { name: "Change history" })).toContainText("Create table data.notes");
  await expect(page).toHaveURL(new RegExp(`^[^?#]*${link("/data/changes")}`));

  // 5. The run resumes, writes its row and replies in the chat.
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

  // 6. Every model call of its turns wrote a usage row, charged to the agent, and so did the
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
});
