import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Run pages are read-only: a run's conversation, its turns and its event log, with no way to
 * post to it. People talk to agents by mentioning them in chats, and the run service runs every
 * turn.
 */

test("a run's page shows its conversation, turns and events, and takes no messages", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, "Find sources", 2, { status: "succeeded" });
  run.ui_messages = [
    { id: "u1", role: "user", parts: [{ type: "text", text: "Find sources" }] },
    { id: "a1", role: "assistant", parts: [{ type: "text", text: "Here are three.", state: "done" }] },
  ];
  api.addTurn(run, { status: "succeeded", model_calls: 1, tool_calls: 0 });
  await page.goto(inOrg(`/agents/${agent.id}/runs/${run.id}`));

  const conversation = page.getByRole("log", { name: "Conversation" });
  await expect(conversation).toContainText("Find sources");
  await expect(conversation).toContainText("Here are three.");
  await expect(page.getByRole("note", { name: "Read-only" })).toContainText(
    "mention Researcher in a chat",
  );
  // Nothing to post with.
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /send|stop/i })).toHaveCount(0);

  await page.getByRole("tab", { name: "Turns" }).click();
  await expect(page.getByRole("listitem").filter({ hasText: /^Turn / })).toHaveCount(1);
  await page.getByRole("tab", { name: "Event log" }).click();
  await expect(page.getByRole("tab", { name: "Event log" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("textbox")).toHaveCount(0);
});

test("a run with nothing in it yet says how to talk to its agent", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, null, 0);
  await page.goto(inOrg(`/agents/${agent.id}/runs/${run.id}`));

  await expect(page.getByRole("log", { name: "Conversation" })).toContainText("Nothing has happened in this run yet.");
  await expect(page.getByRole("textbox")).toHaveCount(0);
});

test("a run going on the run service is read again until its turn ends", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, "Find sources", 1, { status: "running" });
  run.ui_messages = [{ id: "u1", role: "user", parts: [{ type: "text", text: "Find sources" }] }];
  await page.goto(inOrg(`/agents/${agent.id}/runs/${run.id}`));
  await expect(page.getByRole("log", { name: "Conversation" })).toContainText("Find sources");

  run.ui_messages = [
    ...run.ui_messages,
    { id: "a1", role: "assistant", parts: [{ type: "text", text: "Done looking.", state: "done" }] },
  ];
  run.status = "succeeded";
  await expect(page.getByRole("log", { name: "Conversation" })).toContainText("Done looking.");
});

test("an agent's page has no new run to type into", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(inOrg(`/agents/${agent.id}`));

  await expect(page.getByRole("heading", { name: /Researcher/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "New run" })).toHaveCount(0);
});
