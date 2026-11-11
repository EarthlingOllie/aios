import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Archiving a skill from its page: one confirmation that lists, read-only, the agents using it,
 * and is never blocked, even while they do. An archived skill carries a badge and leaves the
 * default Skills list for the Archived filter; an agent whose current version uses it says so.
 */

const dialog = (page: Page) => page.getByRole("dialog", { name: /Archive/ });
const links = (page: Page) => page.getByRole("list", { name: "Skills" }).getByRole("link");
const show = (page: Page, name: string) =>
  page.getByRole("group", { name: "Show" }).getByRole("button", { name, exact: true });

test("a skill in use is archived in one confirmation that lists its agents, and unarchived", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes");
  api.addSkill("Tone");
  api.addSkillUse(skill, api.addAgent("Writer"));
  await page.goto(inOrg(`/skills/${skill.id}`));

  await page.getByRole("button", { name: "Archive", exact: true }).click();
  await expect(dialog(page).getByLabel("Used by").getByRole("link", { name: "Writer" })).toBeVisible();
  await dialog(page).getByRole("button", { name: "Archive skill" }).click();

  await expect(dialog(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Release notes/ }).getByText("archived")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("This skill is archived by Terry Tester");
  await expect(links(page)).toHaveText([/Tone/]);
  await show(page, "Archived").click();
  await expect(links(page)).toHaveText([/Release notes.*archived/]);

  await page.getByRole("button", { name: "Unarchive" }).click();
  await expect(page.getByRole("status")).toHaveCount(0);
  await expect(links(page)).toHaveCount(0);
  expect(api.writes).toEqual([
    ["POST", orgApi(`/skills/${skill.id}/archive`), null],
    ["POST", orgApi(`/skills/${skill.id}/unarchive`), null],
  ]);
});

test("an agent says when a skill its current version uses is archived", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Writer");
  const archived = api.addSkill("Release notes");
  const live = api.addSkill("Tone");
  api.addSkillUse(archived, agent);
  api.addSkillUse(live, agent);
  archived.archived_at = "2026-11-10T09:00:00Z";
  archived.archived_by = api.me;
  await page.goto(inOrg(`/agents/${agent.id}`));

  await expect(page.getByRole("note")).toContainText("The skill Release notes is archived");
  const skills = page.getByRole("tabpanel").getByRole("listitem");
  await expect(skills.filter({ hasText: "Release notes" })).toContainText("archived");
  await expect(skills.filter({ hasText: "Tone" })).not.toContainText("archived");

  // The edit form keeps it attached, marked, until it's removed.
  await page.getByRole("link", { name: "Edit" }).click();
  const attached = page.getByRole("checkbox", { name: /Release notes/ });
  await expect(attached).toBeChecked();
  await expect(page.getByText("archived, remove to make a new version")).toBeVisible();
});
