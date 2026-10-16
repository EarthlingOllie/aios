import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";

/** The workflow page splits its detail into tabs: Graph, Runs, Versions and Schedule. */

const tab = (page: Page, name: string | RegExp) => page.getByRole("tab", { name });
const panel = (page: Page) => page.getByRole("tabpanel");
const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);

function seed(api: FakeApi) {
  const researcher = api.addAgent("Researcher");
  const writer = api.addAgent("Writer");
  return api.addWorkflow(
    "Report",
    [
      { key: "research", agent: researcher },
      { key: "write", agent: writer },
    ],
    [["research", "write"]],
  );
}

test("opens on the Graph tab, with the workflow's graph", async ({ page }) => {
  const api = await fakeApi(page);
  const report = seed(api);
  api.addRun(report, { status: "succeeded", input: "First run", output: "ok" });
  await page.goto(`/workflows/${report.id}`);

  await expect(tab(page, "Graph")).toHaveAttribute("aria-selected", "true");
  await expect(panel(page).locator(node(page, "research"))).toBeVisible();
  // The other tabs' content isn't shown.
  await expect(page.getByText("First run")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}$`));
});

test("switches tabs, keeps the tab in the URL, and shows counts", async ({ page }) => {
  const api = await fakeApi(page);
  const report = seed(api);
  api.addWorkflowVersion(report, (v) => (v.edges = []), "unlink");
  api.addRun(report, { status: "succeeded", input: "First run", output: "ok" });
  api.addRun(report, { status: "failed", input: "Second run", error: "x" });
  await page.goto(`/workflows/${report.id}`);

  await expect(tab(page, "Runs 2")).toBeVisible();
  await expect(tab(page, "Versions 2")).toBeVisible();

  await tab(page, /Runs/).click();
  await expect(page).toHaveURL(/\?tab=runs$/);
  await expect(panel(page).getByRole("link", { name: /Second run/ })).toBeVisible();
  await expect(node(page, "research")).toHaveCount(0);

  await tab(page, /Versions/).click();
  await expect(page).toHaveURL(/\?tab=versions$/);
  await expect(panel(page).getByText("unlink")).toBeVisible();

  await tab(page, "Schedule").click();
  await expect(page).toHaveURL(/\?tab=schedule$/);
  await expect(
    panel(page).getByText("No schedules. Add one to run this workflow automatically."),
  ).toBeVisible();

  // A reload, or a shared link, opens on the same tab.
  await page.reload();
  await expect(tab(page, "Schedule")).toHaveAttribute("aria-selected", "true");

  // Back to the first tab drops the parameter.
  await tab(page, "Graph").click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}$`));
});

test("going back from a run or a version returns to its tab", async ({ page }) => {
  const api = await fakeApi(page);
  const report = seed(api);
  api.addWorkflowVersion(report, (v) => (v.edges = []), "unlink");
  api.addRun(report, { status: "succeeded", input: "First run", output: "ok" });
  await page.goto(`/workflows/${report.id}?tab=runs`);

  await panel(page)
    .getByRole("link", { name: /First run/ })
    .click();
  await page.getByRole("heading").getByRole("link", { name: "Report" }).click();
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");

  await tab(page, /Versions/).click();
  await panel(page).getByRole("link", { name: /v1/ }).click();
  await page.getByRole("heading").getByRole("link", { name: "Report" }).click();
  await expect(tab(page, /Versions/)).toHaveAttribute("aria-selected", "true");
});

test("a run can be started from any tab", async ({ page }) => {
  const api = await fakeApi(page);
  const report = seed(api);
  await page.goto(`/workflows/${report.id}?tab=versions`);

  await page.getByRole("button", { name: "Run" }).click();
  await page.getByLabel("Input").fill("Summarise this week's tickets");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+\/runs\/\d+$/);
});
