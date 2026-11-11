import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** The skill page splits its detail into tabs: Content, Used by and Versions. */

const tab = (page: Page, name: string | RegExp) => page.getByRole("tab", { name });
const panel = (page: Page) => page.getByRole("tabpanel");

test("opens on the Content tab, with when to use the skill and its instructions", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes");
  api.addSkillUse(skill, api.addAgent("Writer"));
  await page.goto(inOrg(`/skills/${skill.id}`));

  await expect(page.getByRole("tablist")).toBeVisible();
  await expect(tab(page, /Content/)).toHaveAttribute("aria-selected", "true");
  await expect(tab(page, /Used by/)).toHaveAttribute("aria-selected", "false");
  await expect(panel(page).getByRole("heading", { name: "When to use" })).toBeVisible();
  await expect(panel(page).getByRole("heading", { name: "Instructions" })).toBeVisible();
  await expect(panel(page).getByText("Release notes instructions, v1.")).toBeVisible();
  // The title and actions stay above the tabs.
  await expect(page.getByRole("link", { name: "Edit" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Archive", exact: true })).toBeVisible();
  // The other tabs' content isn't shown.
  await expect(page.getByRole("link", { name: /Writer/ })).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/skills/${skill.id}$`));
});

test("switches tabs on click, keeps the tab in the URL, and shows counts", async ({ page }) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes", 3);
  api.addSkillUse(skill, api.addAgent("Writer"), 2);
  api.addSkillUse(skill, api.addAgent("Editor"));
  await page.goto(inOrg(`/skills/${skill.id}`));

  await expect(tab(page, "Used by 2")).toBeVisible();
  await expect(tab(page, "Versions 3")).toBeVisible();

  await tab(page, /Used by/).click();
  await expect(tab(page, /Used by/)).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/\/used-by$/);
  await expect(panel(page).getByRole("link", { name: /Writer/ })).toContainText(
    "update available",
  );
  await expect(panel(page).getByRole("link", { name: /Editor/ })).toBeVisible();
  await expect(page.getByText("Release notes instructions, v3.")).toHaveCount(0);

  await tab(page, /Versions/).click();
  await expect(page).toHaveURL(/\/versions$/);
  await expect(panel(page).getByRole("link")).toHaveCount(3);
  await expect(panel(page).getByText("current")).toBeVisible();

  // A reload, or a shared link, opens on the same tab.
  await page.reload();
  await expect(tab(page, /Versions/)).toHaveAttribute("aria-selected", "true");

  // Back to the first tab drops the parameter.
  await tab(page, /Content/).click();
  await expect(page).toHaveURL(new RegExp(`/skills/${skill.id}$`));
});

test("a deep link opens the Used by tab; with no agents it says so", async ({ page }) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes");
  await page.goto(inOrg(`/skills/${skill.id}/used-by`));

  await expect(tab(page, "Used by 0")).toHaveAttribute("aria-selected", "true");
  await expect(panel(page).getByText("No agents use this skill yet.")).toBeVisible();
});

test("an unknown tab in the URL falls back to the first", async ({ page }) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes");
  await page.goto(inOrg(`/skills/${skill.id}/nope`));
  await expect(tab(page, /Content/)).toHaveAttribute("aria-selected", "true");
});

test("moves between tabs with the keyboard", async ({ page }) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes");
  await page.goto(inOrg(`/skills/${skill.id}`));

  await tab(page, /Content/).focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, /Used by/)).toBeFocused();
  await expect(tab(page, /Used by/)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tab(page, /Versions/)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(tab(page, /Content/)).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: /Content/ })).toBeVisible();
});

test("going back from a version returns to the Versions tab", async ({ page }) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes", 2);
  await page.goto(inOrg(`/skills/${skill.id}/versions`));

  await panel(page).getByRole("link").first().click();
  await expect(page).toHaveURL(new RegExp(`/skills/${skill.id}/versions/2$`));
  await page.getByRole("heading").getByRole("link", { name: "Release notes" }).click();
  await expect(page).toHaveURL(new RegExp(`/skills/${skill.id}/versions$`));
  await expect(tab(page, /Versions/)).toHaveAttribute("aria-selected", "true");
});

test("restoring a version returns to the Versions tab, with the new current version", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const skill = api.addSkill("Release notes", 2);
  await page.goto(inOrg(`/skills/${skill.id}/versions`));

  await panel(page).getByRole("link", { name: /v1/ }).click();
  await page.getByRole("button", { name: "Restore this version" }).click();

  await expect(page).toHaveURL(new RegExp(`/skills/${skill.id}/versions$`));
  await expect(tab(page, "Versions 3")).toHaveAttribute("aria-selected", "true");
  const restored = panel(page).getByRole("link", { name: /v3/ });
  await expect(restored).toContainText("Restored from v1");
  await expect(restored).toContainText("current");
});
