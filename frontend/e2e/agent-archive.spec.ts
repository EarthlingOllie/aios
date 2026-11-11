import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Archiving an agent from its page: one confirmation that shows, read-only, what uses it, and is
 * never blocked. An archived agent carries a badge, leaves the default Agents list for the
 * Archived filter, shows as archived among a chat's members and isn't offered to mention.
 * Unarchiving brings it all back.
 */

const dialog = (page: Page) => page.getByRole("dialog", { name: /Archive/ });
const links = (page: Page) => page.getByRole("list", { name: "Agents" }).getByRole("link");
const show = (page: Page, name: string) =>
  page.getByRole("group", { name: "Show" }).getByRole("button", { name, exact: true });

test("an agent is archived in one confirmation that lists what uses it, and unarchived", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Builder");
  api.addAgent("Other");
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "agent", agent);
  api.addSchedule("agent", agent);
  api.agentDependents.set(agent.id, {
    agent_runs: [],
    workflow_runs: [],
    workflows: [{ id: 51, name: "Nightly build", version: 3, direct: false, triggers: [] }],
    triggers: [
      { id: 81, enabled: true, cron: "0 * * * *", description: "Every hour", timezone: "UTC", next_run_at: null },
    ],
  });
  await page.goto(inOrg(`/agents/${agent.id}`));

  await page.getByRole("button", { name: "Archive", exact: true }).click();
  const usedBy = dialog(page).getByLabel("Used by");
  await expect(usedBy).toContainText("Nightly build (through a nested workflow)");
  await expect(usedBy).toContainText("Every hour (UTC)");
  await expect(usedBy.getByRole("link", { name: "Design" })).toBeVisible();
  await expect(usedBy).toContainText(/Data tables left read-only\s*None/);
  await dialog(page).getByRole("button", { name: "Archive agent" }).click();

  await expect(dialog(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Builder/ }).getByText("archived")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("This agent is archived by Terry Tester");
  await expect(page.getByRole("button", { name: "New run" })).toHaveCount(0);
  // The default list hides it; the Archived filter shows it.
  await expect(links(page)).toHaveText([/Other/]);
  await show(page, "Archived").click();
  await expect(page).toHaveURL(inOrg(`/agents/${agent.id}?filter=archived`));
  await expect(links(page)).toHaveText([/Builder.*archived/]);

  await page.getByRole("button", { name: "Unarchive" }).click();
  await expect(page.getByRole("button", { name: "New run" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0);
  await expect(links(page)).toHaveCount(0);
  await show(page, "All").click();
  await expect(links(page)).toHaveText([/Builder/, /Other/]);
  expect(api.writes).toEqual([
    ["POST", orgApi(`/agents/${agent.id}/archive`), null],
    ["POST", orgApi(`/agents/${agent.id}/unarchive`), null],
  ]);
});

test("an archived agent shows as archived among chat members and isn't offered to mention", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  const archived = api.addAgent("Builder");
  const other = api.addAgent("Reviewer");
  api.addChatTarget(chat, "agent", archived);
  api.addChatTarget(chat, "agent", other);
  archived.archived_at = "2026-11-10T09:00:00Z";
  archived.archived_by = api.me;
  await page.goto(inOrg(`/chats/${chat.id}`));

  await page.getByRole("textbox", { name: "Message", exact: true }).pressSequentially("hi @");
  const suggestions = page.getByRole("listbox", { name: "Mention" });
  await expect(suggestions.getByRole("group").filter({ hasText: "Agents" })).toContainText("Reviewer");
  await expect(suggestions).not.toContainText("Builder");

  await page.getByRole("tab", { name: /Members/ }).click();
  const agents = page.getByRole("tabpanel", { name: /Members/ }).getByRole("list", { name: "Agents" });
  await expect(agents.getByRole("listitem").filter({ hasText: "Builder" })).toContainText("archived");
  await expect(agents.getByRole("listitem").filter({ hasText: "Reviewer" })).not.toContainText("archived");
});
