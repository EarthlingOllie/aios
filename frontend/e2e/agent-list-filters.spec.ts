import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * The Agents list can be narrowed to the agents whose current version uses a skill, or has a
 * tool, kept in the URL as `?skill=<id>` and `?tool=<key>`.
 */

const links = (page: Page) => page.getByRole("list", { name: "Agents" }).getByRole("link");
const skillSelect = (page: Page) => page.getByRole("combobox", { name: "Skill" });
const toolSelect = (page: Page) => page.getByRole("combobox", { name: "Tool" });

test("the agents can be filtered by a skill and by a tool, with the search", async ({ page }) => {
  const api = await fakeApi(page);
  api.addTool("web_search", "Web search");
  api.addTool("run_python", "Run Python");
  const terse = api.addSkill("Terse");
  api.addSkill("Haiku");
  const alpha = api.addAgent("Alpha writer");
  const beta = api.addAgent("Beta writer");
  const gamma = api.addAgent("Gamma");
  api.addSkillUse(terse, alpha);
  api.addSkillUse(terse, gamma);
  api.addToolUse(beta, "web_search");
  api.addToolUse(gamma, "web_search");
  await page.goto(inOrg("/agents"));

  await expect(skillSelect(page)).toHaveValue("");
  await expect(skillSelect(page).getByRole("option")).toHaveText(["Any skill", "Terse", "Haiku"]);
  await skillSelect(page).selectOption({ label: "Terse" });
  await expect(page).toHaveURL(inOrg(`/agents?skill=${terse.id}`));
  await expect(links(page)).toHaveText([/Alpha writer/, /Gamma/]);
  await expect(page.getByText("2 of 3")).toBeVisible();

  // The tool narrows it further, and so does the search.
  await expect(toolSelect(page).getByRole("option")).toHaveText(["Any tool", "Web search", "Run Python"]);
  await toolSelect(page).selectOption({ label: "Web search" });
  await expect(page).toHaveURL(inOrg(`/agents?skill=${terse.id}&tool=web_search`));
  await expect(links(page)).toHaveText([/Gamma/]);

  await skillSelect(page).selectOption({ label: "Any skill" });
  await expect(page).toHaveURL(inOrg("/agents?tool=web_search"));
  await expect(links(page)).toHaveText([/Beta writer/, /Gamma/]);
  await page.getByRole("searchbox", { name: "Search agents" }).fill("writer");
  await expect(links(page)).toHaveText([/Beta writer/]);

  // Opening an agent keeps the filters.
  await links(page).first().click();
  await expect(page).toHaveURL(inOrg(`/agents/${beta.id}?q=writer&tool=web_search`));
  await expect(toolSelect(page)).toHaveValue("web_search");
});

test("a link with a skill opens filtered, and an unknown skill or tool is as if none", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addTool("web_search", "Web search");
  const terse = api.addSkill("Terse");
  const alpha = api.addAgent("Alpha");
  api.addAgent("Beta");
  api.addSkillUse(terse, alpha);

  // Filtered even before the skills have loaded.
  let releaseSkills!: () => void;
  const skillsHeld = new Promise<void>((resolve) => (releaseSkills = resolve));
  await page.route(`**${orgApi("/skills")}`, async (route) => {
    await skillsHeld;
    await route.fallback();
  });
  await page.goto(inOrg(`/agents?skill=${terse.id}`));
  await expect(links(page)).toHaveText([/Alpha/]);
  await expect(page.getByText("1 of 2")).toBeVisible();
  releaseSkills();
  await expect(skillSelect(page).getByRole("option", { name: "Terse" })).toBeAttached();
  await expect(skillSelect(page)).toHaveValue(String(terse.id));
  await expect(links(page)).toHaveText([/Alpha/]);

  await page.goto(inOrg("/agents?skill=999999&tool=gone"));
  await expect(links(page)).toHaveText([/Alpha/, /Beta/]);
  await expect(skillSelect(page)).toHaveValue("");
  await expect(toolSelect(page)).toHaveValue("");
});
