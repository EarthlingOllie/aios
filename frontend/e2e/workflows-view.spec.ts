import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";

const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);
const edge = (page: Page, source: string, target: string) =>
  page.locator(`.react-flow__edge[data-id="${source}->${target}"]`);
/** Clicks halfway along an edge, as a user would: a straight edge's box has no height, so
 * Playwright doesn't consider the edge element itself visible. */
async function clickEdge(page: Page, source: string, target: string) {
  const from = (await node(page, source).boundingBox())!;
  const to = (await node(page, target).boundingBox())!;
  await page.mouse.click(
    (from.x + from.width + to.x) / 2,
    (from.y + from.height / 2 + to.y + to.height / 2) / 2,
  );
}
const inspector = (page: Page) => page.getByRole("complementary", { name: "Inspector" });

async function seed(api: FakeApi) {
  const researcher = api.addAgent("Researcher", 1, "Finds sources");
  const writer = api.addAgent("Writer");
  const critic = api.addAgent("Critic");
  const review = api.addWorkflow("Review", [{ key: "critic", agent: critic }]);
  const report = api.addWorkflow(
    "Report",
    [
      { key: "research", agent: researcher },
      { key: "write", agent: writer },
      { key: "review", workflow: review },
    ],
    [
      ["research", "write"],
      ["write", "review"],
    ],
  );
  return { researcher, writer, critic, review, report };
}

test("lists workflows and shows a workflow's graph", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  await page.goto("/workflows");

  const list = page.getByRole("list").filter({ hasText: "Report" });
  await expect(list.getByText("3 nodes")).toBeVisible();
  await page.getByRole("link", { name: /Report/ }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  await expect(page.getByRole("heading", { name: "Report" })).toBeVisible();

  for (const [key, name] of [
    ["research", "Researcher"],
    ["write", "Writer"],
    ["review", "Review"],
  ]) {
    await expect(node(page, key)).toContainText(name);
    await expect(node(page, key)).toContainText(key);
  }
  await expect(node(page, "research")).toContainText("Start");
  await expect(node(page, "review")).toContainText("Workflow");
  await expect(edge(page, "research", "write")).toBeAttached();
  await expect(edge(page, "write", "review")).toBeAttached();

  // Laid out left to right in order of execution.
  const x = async (key: string) => (await node(page, key).boundingBox())!.x;
  expect(await x("research")).toBeLessThan(await x("write"));
  expect(await x("write")).toBeLessThan(await x("review"));
});

test("clicking a node shows what it runs", async ({ page }) => {
  const api = await fakeApi(page);
  const { report, writer } = await seed(api);
  api.bumpAgent(writer);
  await page.goto(`/workflows/${report.id}`);

  await expect(inspector(page)).toContainText("Select a node or an edge");
  await node(page, "write").click();
  const panel = inspector(page);
  await expect(panel).toContainText("write");
  await expect(panel).toContainText("Agent");
  await expect(panel.getByRole("link", { name: "Writer" })).toHaveAttribute(
    "href",
    `/agents/${writer.id}`,
  );
  await expect(panel).toContainText("v1");
  await expect(panel).toContainText("v2 available");
  await expect(panel).toContainText("After research");
  await expect(panel).toContainText("Then review");
  // Nodes with an update are flagged on the canvas too.
  await expect(node(page, "write")).toContainText("v2 available");

  await node(page, "review").click();
  await expect(panel.getByRole("link", { name: "Review" })).toHaveAttribute(
    "href",
    /\/workflows\/\d+$/,
  );
  await expect(panel).toContainText("Workflow");

  // It opens in this tab: there's nothing unsaved here to lose.
  await expect(panel.getByRole("link", { name: "Review" })).not.toHaveAttribute("target", /.*/);
  await panel.getByRole("link", { name: "Review" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+$/);
  await expect(page).not.toHaveURL(`/workflows/${report.id}`);
});

test("a node whose target was deleted names it without a link", async ({ page }) => {
  const api = await fakeApi(page);
  const { report, review } = await seed(api);
  review.deleted = true;
  await page.goto(`/workflows/${report.id}`);

  await node(page, "review").click();
  const panel = inspector(page);
  await expect(panel).toContainText("Review");
  await expect(panel).toContainText("deleted");
  // Its page is gone, so there's nothing to link to.
  await expect(panel.getByRole("link", { name: "Review" })).toHaveCount(0);
});

test("clicking an edge shows what passes along it", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  await page.goto(`/workflows/${report.id}`);

  await clickEdge(page, "research", "write");
  const panel = inspector(page);
  await expect(panel).toContainText("Edge");
  await expect(panel).toContainText("research → write");
  await expect(panel).toContainText("When research finishes, its output is passed to write");

  // The selected edge stands out from the rest: drawn thicker, with its own arrowhead.
  const path = (source: string, target: string) =>
    edge(page, source, target).locator(".react-flow__edge-path");
  await expect(path("research", "write")).toHaveCSS("stroke-width", "2.5px");
  await expect(path("write", "review")).toHaveCSS("stroke-width", "1px");
  const marker = (source: string, target: string) =>
    path(source, target).getAttribute("marker-end");
  expect(await marker("research", "write")).not.toEqual(await marker("write", "review"));

  // Clicking the empty canvas clears the selection.
  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
  await expect(panel).toContainText("Select a node or an edge");
  await expect(path("research", "write")).toHaveCSS("stroke-width", "1px");
});

test("saved positions are used for the layout", async ({ page }) => {
  const api = await fakeApi(page);
  const a = api.addAgent("A");
  const wf = api.addWorkflow(
    "Laid out",
    [
      { key: "first", agent: a },
      { key: "second", agent: a },
    ],
    [["first", "second"]],
    // Drawn right to left, against the automatic layout.
    { first: { x: 600, y: 0 }, second: { x: 0, y: 0 } },
  );
  await page.goto(`/workflows/${wf.id}`);
  const first = (await node(page, "first").boundingBox())!;
  const second = (await node(page, "second").boundingBox())!;
  expect(second.x).toBeLessThan(first.x);
});

test("a graph too wide to fit is shown legibly from where it starts, to be panned", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const api = await fakeApi(page);
  const a = api.addAgent("A");
  const keys = ["one", "two", "three", "four", "five"];
  const wf = api.addWorkflow(
    "Long",
    keys.map((key) => ({ key, agent: a })),
    keys.slice(1).map((key, i) => [keys[i], key]),
  );
  await page.goto(`/workflows/${wf.id}`);
  // Drawn at no less than 80% of its size: its text is still readable.
  const first = (await node(page, "one").boundingBox())!;
  expect(first.width).toBeGreaterThanOrEqual(160);
  // The start is in view; the canvas isn't much taller than the row of nodes.
  const canvas = (await page.locator(".react-flow").boundingBox())!;
  expect(first.x).toBeGreaterThan(canvas.x);
  expect(canvas.height).toBeLessThan(320);
});

test("an empty workflow says so", async ({ page }) => {
  const api = await fakeApi(page);
  const wf = api.addWorkflow("Empty");
  await page.goto(`/workflows/${wf.id}`);
  await expect(page.getByText("This workflow has no nodes yet.")).toBeVisible();
});

test("version history opens an old version's graph, which can be restored", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  api.addWorkflowVersion(
    report,
    (v) => {
      v.nodes = v.nodes.filter((n) => n.key !== "review");
      v.edges = v.edges.filter((e) => e.target !== "review");
    },
    "drop review",
  );
  await page.goto(`/workflows/${report.id}`);
  await expect(node(page, "review")).toHaveCount(0);

  await page.getByRole("tab", { name: /Versions/ }).click();
  const history = page.getByRole("list").filter({ hasText: "drop review" });
  await expect(history.getByText("2 nodes")).toBeVisible();
  await history.getByRole("link", { name: /v1/ }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/versions/1`);
  await expect(node(page, "review")).toBeVisible();
  await node(page, "review").click();
  await expect(inspector(page)).toContainText("Review");

  await page.getByRole("button", { name: "Restore this version" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/versions`);
  await expect(page.getByText("Restored from v1")).toBeVisible();
  await page.getByRole("tab", { name: /Graph/ }).click();
  await expect(node(page, "review")).toBeVisible();
});

/** Holds restore requests until `release` is called, counting them. */
async function holdRestores(page: Page) {
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  const restores = { count: 0, release };
  await page.route("**/api/workflows/*/versions/*/restore", async (route) => {
    restores.count++;
    await held;
    await route.fallback();
  });
  return restores;
}

test("restoring a version sends one request, however often it's clicked", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  api.addWorkflowVersion(report, (v) => (v.nodes = []), "empty");
  const restores = await holdRestores(page);
  await page.goto(`/workflows/${report.id}/versions/1`);

  await page.getByRole("button", { name: "Restore this version" }).dblclick();
  await expect.poll(() => restores.count).toBeGreaterThan(0);
  restores.release();
  await expect(page).toHaveURL(`/workflows/${report.id}/versions`);
  expect(restores.count).toBe(1);
  expect(api.writes.filter(([method]) => method === "POST")).toHaveLength(1);
});

test("the restore button shows that the restore is running", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  api.addWorkflowVersion(report, (v) => (v.nodes = []), "empty");
  const restores = await holdRestores(page);
  await page.goto(`/workflows/${report.id}/versions/1`);

  await page.getByRole("button", { name: "Restore this version" }).click();
  const restoring = page.getByRole("button", { name: "Restoring…" });
  await expect(restoring).toBeDisabled();
  restores.release();
  await expect(page).toHaveURL(`/workflows/${report.id}/versions`);
});

test("a failed restore can be retried", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  api.addWorkflowVersion(report, (v) => (v.nodes = []), "empty");
  let failures = 1;
  await page.route("**/api/workflows/*/versions/*/restore", (route) =>
    failures-- > 0
      ? route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Restore failed" }),
        })
      : route.fallback(),
  );
  await page.goto(`/workflows/${report.id}/versions/1`);

  await page.getByRole("button", { name: "Restore this version" }).click();
  await expect(page.getByText("Restore failed")).toBeVisible();
  await page.getByRole("button", { name: "Restore this version" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/versions`);
  await expect(page.getByText("Restored from v1")).toBeVisible();
});

test("version history that fails to load says why", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  await page.route(`**/api/workflows/${report.id}/versions`, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Version store unavailable" }),
    }),
  );
  await page.goto(`/workflows/${report.id}/versions`);
  await expect(page.getByText("Version store unavailable")).toBeVisible();
});

test("a version page whose workflow fails to load says why", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  // Registered after the fake API's route, so it's tried first.
  await page.route(`**/api/workflows/${report.id}`, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Workflow store unavailable" }),
    }),
  );
  await page.goto(`/workflows/${report.id}/versions/1`);
  await expect(page.getByText("Workflow store unavailable")).toBeVisible();
});

test("deleting a workflow that another runs is refused with the reason", async ({ page }) => {
  const api = await fakeApi(page);
  const { review } = await seed(api);
  await page.goto(`/workflows/${review.id}`);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete workflow" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Remove this workflow from these workflows first: Report.",
  );
});

test("a refused delete's reason doesn't follow you to another workflow", async ({ page }) => {
  const api = await fakeApi(page);
  const { review, report } = await seed(api);
  await page.goto(`/workflows/${review.id}`);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete workflow" }).click();
  await expect(page.getByRole("alert")).toBeVisible();

  await page.getByRole("link", { name: /Report/ }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  await expect(page.getByRole("heading", { name: "Report" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("deleting a workflow returns to the list", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = await seed(api);
  await page.goto(`/workflows/${report.id}`);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete workflow" }).click();
  await expect(page).toHaveURL("/workflows");
  await expect(page.getByRole("link", { name: /Report/ })).toHaveCount(0);
});
