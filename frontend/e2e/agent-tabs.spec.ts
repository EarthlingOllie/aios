import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The agent page splits its detail into tabs: Context, Runs and Versions. */

const tab = (page: Page, name: string | RegExp) => page.getByRole("tab", { name });
const panel = (page: Page) => page.getByRole("tabpanel");

test("opens on the Context tab, with the agent's skills, tools and prompt", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  api.addAgentRun(agent, "Find sources");
  await page.goto(`/agents/${agent.id}`);

  await expect(page.getByRole("tablist")).toBeVisible();
  await expect(tab(page, /Context/)).toHaveAttribute("aria-selected", "true");
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "false");
  await expect(panel(page).getByRole("heading", { name: "Skills" })).toBeVisible();
  await expect(panel(page).getByRole("heading", { name: "Tools" })).toBeVisible();
  await expect(panel(page).getByText("You are Researcher.").first()).toBeVisible();
  // The other tabs' content isn't shown.
  await expect(page.getByText("Find sources")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/agents/${agent.id}$`));
});

test("switches tabs on click, keeps the tab in the URL, and shows counts", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher", 3);
  api.addAgentRun(agent, "Find sources", 4);
  api.addAgentRun(agent, "Summarise");
  await page.goto(`/agents/${agent.id}`);

  await expect(tab(page, "Runs 2")).toBeVisible();
  await expect(tab(page, "Versions 3")).toBeVisible();

  await tab(page, /Runs/).click();
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/\?tab=runs$/);
  await expect(panel(page).getByRole("link", { name: /Find sources/ })).toBeVisible();
  await expect(panel(page).getByRole("link", { name: /Summarise/ })).toBeVisible();
  await expect(page.getByText("You are Researcher.")).toHaveCount(0);

  await tab(page, /Versions/).click();
  await expect(page).toHaveURL(/\?tab=versions$/);
  await expect(panel(page).getByRole("link")).toHaveCount(3);
  await expect(panel(page).getByText("current")).toBeVisible();

  // A reload, or a shared link, opens on the same tab.
  await page.reload();
  await expect(tab(page, /Versions/)).toHaveAttribute("aria-selected", "true");

  // Back to the first tab drops the parameter.
  await tab(page, /Context/).click();
  await expect(page).toHaveURL(new RegExp(`/agents/${agent.id}$`));
});

test("an unknown tab in the URL falls back to the first", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(`/agents/${agent.id}?tab=nope`);
  await expect(tab(page, /Context/)).toHaveAttribute("aria-selected", "true");
});

test("moves between tabs with the keyboard", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(`/agents/${agent.id}`);

  // Only the selected tab is in the tab order; the arrows move between them.
  await expect(tab(page, /Context/)).toHaveAttribute("tabindex", "0");
  await expect(tab(page, /Runs/)).toHaveAttribute("tabindex", "-1");
  await tab(page, /Context/).focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, /Runs/)).toBeFocused();
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tab(page, /Schedule/)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, /Context/)).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(tab(page, /Schedule/)).toBeFocused();
  await page.keyboard.press("Home");
  await expect(tab(page, /Context/)).toHaveAttribute("aria-selected", "true");

  // The panel is labelled by its tab.
  await expect(page.getByRole("tabpanel", { name: /Context/ })).toBeVisible();
});

test("going back from a run or a version returns to its tab", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher", 2);
  api.addAgentRun(agent, "Find sources");
  await page.goto(`/agents/${agent.id}?tab=runs`);

  await panel(page)
    .getByRole("link", { name: /Find sources/ })
    .click();
  await page.getByRole("link", { name: "Back to agent" }).click();
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");

  await tab(page, /Versions/).click();
  await panel(page).getByRole("link").first().click();
  await page.getByRole("heading").getByRole("link", { name: "Researcher" }).click();
  await expect(tab(page, /Versions/)).toHaveAttribute("aria-selected", "true");
});

test("each tab's aria-controls names a panel that is on the page", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(`/agents/${agent.id}?tab=runs`);
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");

  const dangling = await page
    .getByRole("tab")
    .evaluateAll((tabs) =>
      tabs
        .map((t) => t.getAttribute("aria-controls"))
        .filter((id) => id !== null && !document.getElementById(id)),
    );
  expect(dangling).toEqual([]);
  const panelId = await panel(page).getAttribute("id");
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-controls", panelId!);
});

test("restoring a version returns to the Versions tab, with the new current version", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher", 2);
  await page.goto(`/agents/${agent.id}?tab=versions`);

  await panel(page).getByRole("link", { name: /v1/ }).click();
  await page.getByRole("button", { name: "Restore this version" }).click();

  await expect(page).toHaveURL(new RegExp(`/agents/${agent.id}\\?tab=versions$`));
  await expect(tab(page, "Versions 3")).toHaveAttribute("aria-selected", "true");
  const restored = panel(page).getByRole("link", { name: /v3/ });
  await expect(restored).toContainText("Restored from v1");
  await expect(restored).toContainText("current");
});

test("Tab moves from the tab list into the panel, even one with nothing to focus", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(`/agents/${agent.id}?tab=runs`);
  await expect(panel(page).getByText("No runs yet")).toBeVisible();

  await tab(page, /Runs/).focus();
  await page.keyboard.press("Tab");
  await expect(panel(page)).toBeFocused();
});

test("deleting a run from its page returns to the Runs tab, without it", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, "Find sources");
  api.addAgentRun(agent, "Summarise");
  await page.goto(`/agents/${agent.id}/runs/${run.id}`);

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete run" }).click();

  await expect(page).toHaveURL(new RegExp(`/agents/${agent.id}\\?tab=runs$`));
  await expect(tab(page, "Runs 1")).toHaveAttribute("aria-selected", "true");
  await expect(panel(page).getByRole("link", { name: /Summarise/ })).toBeVisible();
  await expect(panel(page).getByText("Find sources")).toHaveCount(0);
});

test("the Schedule tab shows the agent's schedules", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(`/agents/${agent.id}`);

  await tab(page, "Schedule").click();
  await expect(page).toHaveURL(/\?tab=schedule$/);
  await expect(
    panel(page).getByText("No schedules. Add one to run this agent automatically."),
  ).toBeVisible();
  await expect(page.getByText("You are Researcher.")).toHaveCount(0);
  // The tab names the section, so the panel doesn't repeat it as a heading.
  await expect(panel(page).getByRole("heading", { name: "Schedules" })).toHaveCount(0);
});
