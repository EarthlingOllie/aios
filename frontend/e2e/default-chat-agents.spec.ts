import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Default chat agents: the agents that join every new chat, in order, on the Organisation page's
 * Settings tab. The organisation assistant to start with; admins add, remove and reorder them,
 * archived ones are marked and skipped, and everyone else sees the list read-only.
 */

test("the organisation assistant is the default, and joins a new chat", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Default chat agents" });
  await expect(section.getByRole("list", { name: "Default chat agents" }).getByRole("listitem")).toHaveText([
    /Organisation assistant/,
  ]);
});

test("an admin adds and reorders the default agents, and new chats get them in order", async ({ page }) => {
  const api = await fakeApi(page);
  const helper = api.addAgent("Helper");
  const assistant = api.organisationAssistant();
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Default chat agents" });
  const items = section.getByRole("list", { name: "Default chat agents" }).getByRole("listitem");
  await expect(items).toHaveText([/Organisation assistant/]);

  await section.getByRole("combobox", { name: "Add an agent" }).selectOption("Helper");
  await section.getByRole("button", { name: "Add", exact: true }).click();
  await section.getByRole("button", { name: "Move Helper up" }).click();
  await expect(items).toHaveText([/Helper/, /Organisation assistant/]);
  await section.getByRole("button", { name: "Apply" }).click();
  await expect(section.getByRole("status")).toHaveText("Saved");
  expect(api.writes).toEqual([
    ["PUT", orgApi("/organisation/default-chat-agents"), { agent_ids: [helper.id, assistant.id] }],
  ]);

  // A new chat gets them, in order.
  await page.goto(inOrg("/chats/new"));
  await page.getByLabel("Name").fill("Design");
  await page.getByRole("button", { name: "Create chat" }).click();
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByRole("list", { name: "Agents" }).getByRole("listitem")).toHaveText([
    "Helper",
    "Organisation assistant",
  ]);
});

test("an admin removes every default agent, and a new chat has none", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Default chat agents" });
  await section.getByRole("button", { name: "Remove Organisation assistant" }).click();
  await expect(section).toContainText("No agents join new chats.");
  await section.getByRole("button", { name: "Apply" }).click();
  await expect(section.getByRole("status")).toHaveText("Saved");
  expect(api.writes).toEqual([["PUT", orgApi("/organisation/default-chat-agents"), { agent_ids: [] }]]);
  expect(api.defaultChatAgents()).toEqual([]);
});

test("an archived default agent is marked archived and skipped in a new chat", async ({ page }) => {
  const api = await fakeApi(page);
  const helper = api.addAgent("Helper");
  const assistant = api.organisationAssistant();
  api.defaultChatAgentIds = [assistant.id, helper.id];
  assistant.archived_at = "2026-11-10T09:00:00Z";
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Default chat agents" });
  const items = section.getByRole("list", { name: "Default chat agents" }).getByRole("listitem");
  await expect(items).toHaveText([/Organisation assistant.*Archived/, /Helper/]);
  await expect(items.filter({ hasText: "Helper" })).not.toContainText("Archived");

  await page.goto(inOrg("/chats/new"));
  await page.getByLabel("Name").fill("Design");
  await page.getByRole("button", { name: "Create chat" }).click();
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByRole("list", { name: "Agents" }).getByRole("listitem")).toHaveText(["Helper"]);
});

for (const role of ["editor", "participant"] as const) {
  test(`${role === "editor" ? "an" : "a"} ${role} sees the default agents read-only`, async ({ page }) => {
    const api = await fakeApi(page);
    api.actAs(role);
    await page.goto(inOrg("/organisation/settings"));
    const section = page.getByRole("region", { name: "Default chat agents" });
    await expect(section.getByRole("listitem")).toHaveText([/Organisation assistant/]);
    await expect(section.getByRole("combobox")).toHaveCount(0);
    await expect(section.getByRole("button")).toHaveCount(0);
    await expect(section).toContainText("Only admins can change which agents join new chats.");
  });
}
