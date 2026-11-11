import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Archiving a workflow from its page, on the same terms as an agent: one confirmation that shows,
 * read-only, what uses it (the workflows that nest it, its schedules, its chats), and is never
 * blocked. An archived workflow carries a badge, can't be run, and leaves the default Workflows
 * list for the Archived filter. Unarchiving brings it back.
 */

const dialog = (page: Page) => page.getByRole("dialog", { name: /Archive/ });
const links = (page: Page) => page.getByRole("list", { name: "Workflows" }).getByRole("link");
const show = (page: Page, name: string) =>
  page.getByRole("group", { name: "Show" }).getByRole("button", { name, exact: true });

test("a workflow is archived in one confirmation that lists what uses it, and unarchived", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const critic = api.addAgent("Critic");
  const review = api.addWorkflow("Review", [{ key: "critic", agent: critic }]);
  api.addWorkflow("Report", [{ key: "review", workflow: review }]);
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "workflow", review);
  api.addSchedule("workflow", review);
  await page.goto(inOrg(`/workflows/${review.id}`));

  await page.getByRole("button", { name: "Archive", exact: true }).click();
  const usedBy = dialog(page).getByLabel("Used by");
  await expect(usedBy.getByRole("link", { name: "Report" })).toBeVisible();
  await expect(usedBy).toContainText("At 09:00 (UTC)");
  await expect(usedBy.getByRole("link", { name: "Design" })).toBeVisible();
  await dialog(page).getByRole("button", { name: "Archive workflow" }).click();

  await expect(dialog(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Review/ }).getByText("archived")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("This workflow is archived by Terry Tester");
  await expect(page.getByRole("button", { name: "Run" })).toHaveCount(0);
  // The default list hides it; the Archived filter shows it.
  await expect(links(page)).toHaveText([/Report/]);
  await show(page, "Archived").click();
  await expect(links(page)).toHaveText([/Review.*archived/]);

  await page.getByRole("button", { name: "Unarchive" }).click();
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0);
  await expect(links(page)).toHaveCount(0);
  await show(page, "All").click();
  await expect(links(page)).toHaveText([/Report/, /Review/]);
  expect(api.writes).toEqual([
    ["POST", orgApi(`/workflows/${review.id}/archive`), null],
    ["POST", orgApi(`/workflows/${review.id}/unarchive`), null],
  ]);
});

test("an archived workflow shows as archived among chat members and isn't offered to mention", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  const archived = api.addWorkflow("Digest");
  const other = api.addWorkflow("Triage");
  api.addChatTarget(chat, "workflow", archived);
  api.addChatTarget(chat, "workflow", other);
  archived.archived_at = "2026-11-10T09:00:00Z";
  archived.archived_by = api.me;
  await page.goto(inOrg(`/chats/${chat.id}`));

  await page.getByRole("textbox", { name: "Message", exact: true }).pressSequentially("hi @");
  const suggestions = page.getByRole("listbox", { name: "Mention" });
  await expect(suggestions.getByRole("group").filter({ hasText: "Workflows" })).toContainText("Triage");
  await expect(suggestions).not.toContainText("Digest");

  await page.getByRole("tab", { name: /Members/ }).click();
  const workflows = page.getByRole("tabpanel", { name: /Members/ }).getByRole("list", { name: "Workflows" });
  await expect(workflows.getByRole("listitem").filter({ hasText: "Digest" })).toContainText("archived");
  await expect(workflows.getByRole("listitem").filter({ hasText: "Triage" })).not.toContainText("archived");
});
