import { expect, test } from "@playwright/test";

import { type ChatUser, fakeApi, inOrg, WEBHOOK_URL } from "./fake-api";

/** Event triggers' forms: each event type's filters, debounce, webhooks' URL and secret, and how
 * they last went. */

const sam: ChatUser = { id: 2, first_name: "Sam", last_name: "Jones" };

test("an editor makes a document ingested trigger with filters on an agent's Schedule tab", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Reader");
  await page.goto(inOrg(`/agents/${agent.id}/schedule`));

  await page.getByRole("button", { name: "Add event trigger" }).click();
  const form = page.getByRole("form", { name: "New event trigger" });
  await expect(form.getByLabel("When")).toHaveValue("document_ingested");
  await form.getByLabel("Content types").fill("application/PDF, text/*");
  await form.getByRole("checkbox", { name: "Uploads" }).check();
  await form.getByLabel("Instructions").fill("Summarise the new document.");
  await form.getByRole("button", { name: "Create trigger" }).click();

  const row = page.getByTestId("event-trigger-row");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("When a document is ingested");
  await expect(row).toContainText("of type application/pdf, text/* from Uploads");
  await expect(row).toContainText("Waiting for events");
  const [made] = api.schedules;
  expect(made).toMatchObject({
    kind: "event",
    target_kind: "agent",
    target_id: agent.id,
    event_type: "document_ingested",
    filter: { content_types: ["application/pdf", "text/*"], sources: [1] },
    input: "Summarise the new document.",
    fire_on_own_events: false,
  });

  // Editing it opens the event form, as it was saved.
  await row.getByRole("button", { name: "Edit trigger" }).click();
  const editing = page.getByRole("form", { name: "Edit event trigger" });
  await expect(editing.getByLabel("Content types")).toHaveValue("application/pdf, text/*");
  await editing.getByRole("checkbox", { name: /its own agent caused/ }).check();
  await editing.getByRole("button", { name: "Save trigger" }).click();
  await expect(page.getByTestId("event-trigger-row")).toHaveCount(1);
  expect(api.schedules[0].fire_on_own_events).toBe(true);
});

test("a workflow's event trigger takes an input template", async ({ page }) => {
  const api = await fakeApi(page);
  const workflow = api.addWorkflow("Filing");
  await page.goto(inOrg("/scheduler"));

  await page.getByRole("button", { name: "Add event trigger" }).click();
  const form = page.getByRole("form", { name: "New event trigger" });
  await form.getByLabel("Runs").selectOption(`workflow:${workflow.id}`);
  await form.getByLabel("Input template").fill("File {{payload.name}}");
  await form.getByRole("button", { name: "Create trigger" }).click();

  await expect(page.getByTestId("event-trigger-row")).toContainText("Filing");
  expect(api.schedules[0]).toMatchObject({
    target_kind: "workflow",
    input: "File {{payload.name}}",
    filter: {},
  });
});

test("a trigger the loop rules refused, or past its rate, says so", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Reader");
  api.addEventTrigger("agent", agent, {
    last_fired_at: "2026-10-15T12:00:00Z",
    last_status: "rate_limited",
    last_error: "Rate limited: at most 60 runs a minute.",
  });
  api.addEventTrigger("agent", agent, {
    last_fired_at: "2026-10-15T12:00:00Z",
    last_status: "refused",
    last_error: "Refused: it already fired in this chain of causes, so it would loop.",
  });
  await page.goto(inOrg("/scheduler"));

  const rows = page.getByTestId("event-trigger-row");
  await expect(rows.nth(0).getByTestId("last-outcome")).toContainText("Rate limited");
  await expect(rows.nth(0)).toContainText("at most 60 runs a minute");
  await expect(rows.nth(1).getByTestId("last-outcome")).toContainText("refused");
  await expect(rows.nth(1)).toContainText("so it would loop");
});

test("a participant can't make or change triggers", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Reader");
  api.addEventTrigger("agent", agent);
  api.actAs("participant");
  await page.goto(inOrg("/scheduler"));

  await expect(page.getByRole("button", { name: "Add event trigger" })).toBeDisabled();
  await expect(page.getByTestId("event-trigger-row")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Edit trigger" })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: "Pause trigger" })).toBeDisabled();
});

test("a table written trigger filters by table and operation, and gathers a burst", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Reader");
  await page.goto(inOrg(`/agents/${agent.id}/schedule`));

  await page.getByRole("button", { name: "Add event trigger" }).click();
  const form = page.getByRole("form", { name: "New event trigger" });
  await form.getByLabel("When").selectOption("table_written");
  // Only this event type's fields.
  await expect(form.getByLabel("Content types")).toHaveCount(0);
  await form.getByLabel("Tables").fill("orders, data.Invoices");
  await form.getByRole("checkbox", { name: "insert" }).check();
  await form.getByLabel("Gather events for (seconds)").fill("60");
  await form.getByRole("button", { name: "Create trigger" }).click();

  const row = page.getByTestId("event-trigger-row");
  await expect(row).toContainText("When a data table is written");
  await expect(row).toContainText("to orders, invoices by insert");
  await expect(row.getByTestId("debounce")).toHaveText("Gathers events for 60s into one run");
  expect(api.schedules[0]).toMatchObject({
    event_type: "table_written",
    filter: { tables: ["orders", "invoices"], operations: ["insert"] },
    debounce_seconds: 60,
  });
});

test("a chat message trigger listens to a chat its creator isn't in, which adds its agent", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Watcher");
  const chat = api.addChat("Incidents", { owner: sam });
  await page.goto(inOrg("/scheduler"));

  await page.getByRole("button", { name: "Add event trigger" }).click();
  const form = page.getByRole("form", { name: "New event trigger" });
  await form.getByLabel("Runs").selectOption(`agent:${agent.id}`);
  await form.getByLabel("When").selectOption("chat_message");
  // A chat is required.
  await expect(form.getByRole("button", { name: "Create trigger" })).toBeDisabled();
  await form.getByLabel("Chat", { exact: true }).selectOption(String(chat.id));
  await form.getByRole("checkbox", { name: "People" }).check();
  await form.getByLabel("Keywords").fill("outage, down");
  await form.getByRole("button", { name: "Create trigger" }).click();

  const row = page.getByTestId("event-trigger-row");
  await expect(row).toContainText("When a message is posted in a chat");
  await expect(row).toContainText('in Incidents written by user saying "outage" or "down"');
  expect(api.schedules[0]).toMatchObject({
    event_type: "chat_message",
    filter: { chat_id: chat.id, author_kinds: ["user"], keywords: ["outage", "down"] },
  });
  expect(chat.members.some((m) => m.kind === "agent" && "target_id" in m && m.target_id === agent.id)).toBe(true);
  expect(chat.members.some((m) => m.kind === "user" && "user" in m && m.user.id === api.me.id)).toBe(false);
});

test("a webhook trigger shows its URL and secret once, and the secret can be rotated", async ({ page }) => {
  const api = await fakeApi(page);
  const workflow = api.addWorkflow("Orders");
  await page.goto(inOrg(`/workflows/${workflow.id}/schedule`));

  await page.getByRole("button", { name: "Add event trigger" }).click();
  const form = page.getByRole("form", { name: "New event trigger" });
  await form.getByLabel("When").selectOption("webhook_received");
  await form.getByRole("button", { name: "Create trigger" }).click();

  const [made] = api.schedules;
  const secret = page.getByRole("region", { name: "Webhook secret" });
  await expect(secret.getByLabel("URL")).toHaveValue(`${WEBHOOK_URL}token-${made.id}`);
  await expect(secret.getByLabel("Signing secret")).toHaveValue(`whsec_${made.id}_1`);
  await secret.getByRole("button", { name: "I've copied it" }).click();
  await expect(secret).toHaveCount(0);

  // The row shows the URL, never the secret.
  const row = page.getByTestId("event-trigger-row");
  await expect(row).toContainText("When its webhook is called");
  await expect(row.getByTestId("webhook-url")).toContainText(`${WEBHOOK_URL}token-${made.id}`);
  await expect(row).not.toContainText("whsec_");

  page.on("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Rotate secret" }).click();
  await expect(secret.getByLabel("Signing secret")).toHaveValue(`whsec_${made.id}_2`);
  await expect(secret.getByLabel("URL")).toHaveValue(`${WEBHOOK_URL}token-${made.id}-2`);
  await secret.getByRole("button", { name: "I've copied it" }).click();
  await expect(row.getByTestId("webhook-url")).toContainText(`${WEBHOOK_URL}token-${made.id}-2`);
});

test("a webhook trigger can listen to webhook sources instead of having its own URL", async ({ page }) => {
  const api = await fakeApi(page);
  const workflow = api.addWorkflow("Orders");
  const orders = api.addSource("Order feed", "webhook", { interval_seconds: null });
  api.addSource("Price page", "url");
  await page.goto(inOrg(`/workflows/${workflow.id}/schedule`));

  await page.getByRole("button", { name: "Add event trigger" }).click();
  const form = page.getByRole("form", { name: "New event trigger" });
  await form.getByLabel("When").selectOption("webhook_received");
  const webhookSources = form.getByRole("group", { name: "Webhook sources" });
  await expect(webhookSources.getByRole("checkbox")).toHaveCount(1);
  await webhookSources.getByLabel("Order feed").check();
  await form.getByRole("button", { name: "Create trigger" }).click();

  const [made] = api.schedules;
  expect(made.filter).toEqual({ sources: [orders.id] });
  expect(made.webhook_url).toBeNull();
  await expect(page.getByRole("region", { name: "Webhook secret" })).toHaveCount(0);
  await expect(page.getByTestId("event-trigger-row")).toContainText("from Order feed");
});
