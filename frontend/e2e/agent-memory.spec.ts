import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * The agent page's Memory tab: what the agent remembered (its `remember` tool), with where each
 * was learned. Editors and admins edit and delete memories, each audited; participants only see.
 */

const panel = (page: Page) => page.getByRole("tabpanel");
const memory = (page: Page, text: string | RegExp) =>
  panel(page).getByTestId("memory").filter({ hasText: text });

test("lists the agent's memories with where each was learned", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Keeper");
  const chat = api.addChat("Planning");
  const run = api.addAgentRun(agent, "Stock check");
  api.addMemory(agent, "The kitten adoption is on Friday.", { chat_id: chat.id, chat_name: "Planning" });
  api.addMemory(agent, "Kitten food is in the blue cupboard.", { agent_run_id: run.id, run_title: "Stock check" });
  api.addMemory(agent, "Vaccinations are due in May.");
  await page.goto(inOrg(`/agents/${agent.id}/memory`));

  await expect(page.getByRole("tab", { name: "Memory 3" })).toHaveAttribute("aria-selected", "true");
  await expect(memory(page, "adoption").getByText("Learned in the chat")).toBeVisible();
  await expect(memory(page, "adoption").getByRole("link", { name: "Planning" })).toBeVisible();
  await expect(memory(page, "blue cupboard").getByRole("link", { name: "Stock check" })).toHaveAttribute(
    "href",
    new RegExp(`/agents/${agent.id}/runs/${run.id}$`),
  );
  await expect(memory(page, "Vaccinations").getByText("Learned in a run that's no longer kept")).toBeVisible();
});

test("an agent with no memories says so", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Keeper");
  await page.goto(inOrg(`/agents/${agent.id}/memory`));

  await expect(panel(page).getByText("No memories yet.")).toBeVisible();
});

test("an editor edits and deletes a memory, and both are audited", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Keeper");
  api.addMemory(agent, "Kitten food is in the blue cupboard.");
  api.addMemory(agent, "The kitten is called Biscuit.");
  await page.goto(inOrg(`/agents/${agent.id}/memory`));

  await memory(page, "blue cupboard").getByRole("button", { name: "Edit memory" }).click();
  await panel(page).getByRole("textbox", { name: "Memory" }).fill("Kitten food is in the green cupboard.");
  await panel(page).getByRole("button", { name: "Save" }).click();
  await expect(memory(page, "green cupboard").getByText(/Edited by/)).toBeVisible();
  await expect(memory(page, "blue cupboard")).toHaveCount(0);

  page.once("dialog", (dialog) => dialog.accept());
  await memory(page, "Biscuit").getByRole("button", { name: "Delete memory" }).click();
  await expect(memory(page, "Biscuit")).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Memory 1" })).toBeVisible();

  expect(api.auditEntries.map((e) => e.action).sort()).toEqual(["memory.deleted", "memory.edited"]);
});

test("a participant sees the memories but can't edit or delete them", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Keeper");
  api.addMemory(agent, "Kitten food is in the blue cupboard.");
  api.actAs("participant");
  await page.goto(inOrg(`/agents/${agent.id}/memory`));

  await expect(memory(page, "blue cupboard")).toBeVisible();
  await expect(panel(page).getByRole("button", { name: "Edit memory" })).toHaveCount(0);
  await expect(panel(page).getByRole("button", { name: "Delete memory" })).toHaveCount(0);
});
