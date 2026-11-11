import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi, inOrg } from "./fake-api";

/** The workflow page splits its detail into tabs: Graph, Runs, Versions and Schedule. */

const tab = (page: Page, name: string | RegExp) => page.getByRole("tab", { name });
const panel = (page: Page) => page.getByRole("tabpanel");
const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);

function seed(api: FakeApi) {
  const researcher = api.addAgent("Researcher");
  const writer = api.addAgent("Writer");
  const report = api.addWorkflow(
    "Report",
    [
      { key: "research", agent: researcher },
      { key: "write", agent: writer },
    ],
    [["research", "write"]],
  );
  return { researcher, writer, report };
}

test("opens on the Graph tab, with the title and actions above the tabs", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addRun(report, { input: "Write the weekly report" });
  await page.goto(inOrg(`/workflows/${report.id}`));

  await expect(page.getByRole("tablist")).toBeVisible();
  await expect(tab(page, /Graph/)).toHaveAttribute("aria-selected", "true");
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "false");
  await expect(panel(page).locator(node(page, "research"))).toBeVisible();
  // The other tabs' content isn't shown.
  await expect(page.getByText("Write the weekly report")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}$`));

  // The title and actions stay above the tabs.
  await expect(page.getByRole("heading", { name: "Report" })).toBeVisible();
  await expect(panel(page).getByRole("button", { name: "Run" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Edit" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Archive", exact: true })).toBeVisible();
});

test("switches tabs on click, keeps the tab in the URL, and shows counts", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addWorkflowVersion(report, (v) => v.edges.pop(), "Drop the edge");
  api.addRun(report, { input: "Write the weekly report", status: "succeeded" });
  api.addRun(report, { input: "Write the monthly report", status: "succeeded" });
  await page.goto(inOrg(`/workflows/${report.id}`));

  await expect(tab(page, "Runs 2")).toBeVisible();
  await expect(tab(page, "Versions 2")).toBeVisible();

  await tab(page, /Runs/).click();
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/\/runs$/);
  await expect(panel(page).getByRole("link", { name: /weekly report/ })).toBeVisible();
  await expect(panel(page).getByRole("link", { name: /monthly report/ })).toBeVisible();
  await expect(node(page, "research")).toHaveCount(0);

  await tab(page, /Versions/).click();
  await expect(page).toHaveURL(/\/versions$/);
  await expect(panel(page).getByRole("link")).toHaveCount(2);
  await expect(panel(page).getByText("current")).toBeVisible();

  await tab(page, "Schedule").click();
  await expect(page).toHaveURL(/\/schedule$/);
  await expect(
    panel(page).getByText("No schedules. Add one to run this workflow automatically."),
  ).toBeVisible();

  // A reload, or a shared link, opens on the same tab.
  await page.reload();
  await expect(tab(page, "Schedule")).toHaveAttribute("aria-selected", "true");

  // Back to the first tab drops the parameter, and shows the graph again.
  await tab(page, /Graph/).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}$`));
  await expect(node(page, "research")).toBeVisible();
});

test("a deep link opens its tab; an unknown one falls back to Graph", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(inOrg(`/workflows/${report.id}/runs`));
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");
  await expect(panel(page).getByText("No runs yet. Start one to watch it go.")).toBeVisible();

  await page.goto(inOrg(`/workflows/${report.id}/nope`));
  await expect(tab(page, /Graph/)).toHaveAttribute("aria-selected", "true");
});

test("moves between tabs with the keyboard", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(inOrg(`/workflows/${report.id}`));

  await tab(page, /Graph/).focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, /Runs/)).toBeFocused();
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tab(page, /Schedule/)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(tab(page, /Graph/)).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: /Graph/ })).toBeVisible();
});

test("the start-a-run form stays above the tabs, whichever is open", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(inOrg(`/workflows/${report.id}/versions`));

  await page.getByRole("button", { name: "Run" }).click();
  const input = page.getByLabel("Input");
  await expect(input).toBeVisible();
  await expect(panel(page).getByLabel("Input")).toHaveCount(0);
  await input.fill("Write the weekly report");
  await tab(page, /Runs/).click();
  await expect(input).toHaveValue("Write the weekly report");
});

test("the graph's node selection resets on switching back to Graph", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(inOrg(`/workflows/${report.id}`));
  const inspector = page.getByRole("complementary", { name: "Inspector" });

  await node(page, "write").click();
  await expect(inspector).toContainText("Writer");
  await tab(page, /Runs/).click();
  await tab(page, /Graph/).click();
  await expect(inspector).toContainText("Select a node or an edge");
});

test("going back from a run returns to the Runs tab", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addRun(report, { input: "Write the weekly report", status: "succeeded" });
  await page.goto(inOrg(`/workflows/${report.id}/runs`));

  await panel(page)
    .getByRole("link", { name: /weekly report/ })
    .click();
  await page.getByRole("heading").getByRole("link", { name: "Report" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}/runs$`));
  await expect(tab(page, /Runs/)).toHaveAttribute("aria-selected", "true");
});

test("deleting a run from its page returns to the Runs tab, without it", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  const run = api.addRun(report, { input: "Write the weekly report", status: "succeeded" });
  api.addRun(report, { input: "Write the monthly report", status: "succeeded" });
  await page.goto(inOrg(`/workflows/${report.id}/runs/${run.id}`));

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete run" }).click();

  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}/runs$`));
  await expect(tab(page, "Runs 1")).toHaveAttribute("aria-selected", "true");
  await expect(panel(page).getByRole("link", { name: /monthly report/ })).toBeVisible();
  await expect(panel(page).getByText("weekly report")).toHaveCount(0);
});

test("going back from a version returns to the Versions tab", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addWorkflowVersion(report, (v) => v.edges.pop());
  await page.goto(inOrg(`/workflows/${report.id}/versions`));

  await panel(page).getByRole("link", { name: /v1/ }).click();
  await page.getByRole("heading").getByRole("link", { name: "Report" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}/versions$`));
  await expect(tab(page, /Versions/)).toHaveAttribute("aria-selected", "true");
});

test("restoring a version returns to the Versions tab, with the new current version", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addWorkflowVersion(report, (v) => v.edges.pop());
  await page.goto(inOrg(`/workflows/${report.id}/versions`));

  await panel(page).getByRole("link", { name: /v1/ }).click();
  await page.getByRole("button", { name: "Restore this version" }).click();

  await expect(page).toHaveURL(new RegExp(`/workflows/${report.id}/versions$`));
  await expect(tab(page, "Versions 3")).toHaveAttribute("aria-selected", "true");
  const restored = panel(page).getByRole("link", { name: /v3/ });
  await expect(restored).toContainText("Restored from v1");
  await expect(restored).toContainText("current");
});
