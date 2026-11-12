import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** Event triggers' forms: document ingested triggers, with their filters, and how they last went. */

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
