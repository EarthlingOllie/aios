import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/**
 * A searched or filtered list stays that way on an item's other pages: its Edit page, its
 * versions and its runs, and the links back from them to the item's tabs.
 */

const links = (page: Page, name: string) => page.getByRole("list", { name }).getByRole("link");
const panel = (page: Page) => page.getByRole("tabpanel");
const backTo = (page: Page, name: string) => page.getByRole("heading").getByRole("link", { name });

test("an agent's Edit, run and version pages keep the list's search and filter", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher", 2, "Finds sources");
  api.addAgent("Writer");
  api.addAgentRun(agent, "Find sources");
  await page.goto(`/agents/${agent.id}?q=research&filter=used`);
  await expect(links(page, "Agents")).toHaveText([/Researcher/]);

  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page).toHaveURL(`/agents/${agent.id}/edit?q=research&filter=used`);
  await expect(links(page, "Agents")).toHaveText([/Researcher/]);
  await page.goBack();

  await page.getByRole("tab", { name: /Runs/ }).click();
  await panel(page).getByRole("link", { name: /Find sources/ }).click();
  await expect(page).toHaveURL(new RegExp(`/agents/${agent.id}/runs/\\d+\\?q=research&filter=used$`));
  await expect(links(page, "Agents")).toHaveText([/Researcher/]);
  await page.getByRole("link", { name: /Back/ }).click();
  await expect(page).toHaveURL(`/agents/${agent.id}/runs?q=research&filter=used`);

  await page.getByRole("tab", { name: /Versions/ }).click();
  await panel(page).getByRole("link", { name: /v1/ }).click();
  await expect(page).toHaveURL(`/agents/${agent.id}/versions/1?q=research&filter=used`);
  await expect(links(page, "Agents")).toHaveText([/Researcher/]);
  await backTo(page, "Researcher").click();
  await expect(page).toHaveURL(`/agents/${agent.id}/versions?q=research&filter=used`);
});

test("restoring a version keeps the list's search", async ({ page }) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes", 2);
  api.addSkill("Tone");
  await page.goto(`/skills/${skill.id}/versions/1?q=release`);

  await page.getByRole("button", { name: "Restore this version" }).click();
  await expect(page).toHaveURL(`/skills/${skill.id}/versions?q=release`);
  await expect(links(page, "Skills")).toHaveText([/Release notes/]);
});

test("a workflow's Edit, run and version pages keep the list's search", async ({ page }) => {
  const api = await fakeApi(page);
  const writer = api.addAgent("Writer");
  const report = api.addWorkflow("Report", [{ key: "write", agent: writer }]);
  api.addWorkflow("Onboarding");
  api.addWorkflowVersion(report, () => {});
  api.addRun(report, { input: "Write the weekly report", status: "succeeded" });
  await page.goto(`/workflows/${report.id}/runs?q=report`);
  await expect(links(page, "Workflows")).toHaveText([/Report/]);

  await panel(page).getByRole("link", { name: /weekly report/ }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}/runs/\\d+\\?q=report$`));
  await expect(links(page, "Workflows")).toHaveText([/Report/]);
  await backTo(page, "Report").click();
  await expect(page).toHaveURL(`/workflows/${report.id}/runs?q=report`);

  await page.getByRole("tab", { name: /Versions/ }).click();
  await panel(page).getByRole("link", { name: /v1/ }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/versions/1?q=report`);
  await backTo(page, "Report").click();
  await expect(page).toHaveURL(`/workflows/${report.id}/versions?q=report`);

  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/edit?q=report`);
});

test("a skill's and a chat's Edit pages keep the list's search", async ({ page }) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes");
  api.addSkill("Tone");
  await page.goto(`/skills/${skill.id}?q=release`);
  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page).toHaveURL(`/skills/${skill.id}/edit?q=release`);
  await expect(links(page, "Skills")).toHaveText([/Release notes/]);

  const chat = api.addChat("Design review");
  api.addChat("Support");
  await page.goto(`/chats/${chat.id}?q=design`);
  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page).toHaveURL(`/chats/${chat.id}/edit?q=design`);
  await expect(links(page, "Chats")).toHaveText([/Design review/]);
});
