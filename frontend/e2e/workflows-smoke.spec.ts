import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/**
 * A smoke test of the whole workflows feature in one pass: list → view → edit → run → watch it
 * live → open the finished run. Each step checks only that it works; the other specs cover the
 * details.
 */

// Wide enough for the five-column graph to fit the canvas: on a narrower screen the viewer and run
// page start it at its left, so its last column is off the canvas until it's panned to.
test.use({ viewport: { width: 1680, height: 1050 } });

const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);
const edge = (page: Page, source: string, target: string) =>
  page.locator(`.react-flow__edge[data-id="${source}->${target}"]`);
const inspector = (page: Page) => page.getByRole("complementary", { name: "Inspector" });

async function addNode(page: Page, name: string) {
  await page.getByLabel("Add node").click();
  await page.getByRole("option", { name, exact: true }).click();
  await page.getByRole("button", { name: "Add", exact: true }).click();
}

/** Connects two nodes in the editor by dragging from one's handle to the other's. */
async function connect(page: Page, source: string, target: string) {
  const from = (await node(page, source).locator(".react-flow__handle-right").boundingBox())!;
  const to = (await node(page, target).locator(".react-flow__handle-left").boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 10 });
  await page.mouse.up();
  await expect(edge(page, source, target)).toBeAttached();
}

test("lists, views, edits, runs and follows a workflow end to end", async ({ page }) => {
  // The whole feature in one test, including a run followed through several polls: more than
  // the default 30s on a busy machine.
  test.slow();
  const api = await fakeApi(page);
  const researcher = api.addAgent("Researcher");
  const writer = api.addAgent("Writer");
  const checker = api.addAgent("Fact checker");
  const critic = api.addAgent("Critic");
  api.addAgent("Publisher");
  api.addWorkflow("Triage", [{ key: "triage", agent: critic }]);
  const weekly = api.addWorkflow(
    "Weekly report",
    [
      { key: "research", agent: researcher },
      { key: "draft", agent: writer },
      { key: "fact_check", agent: checker },
      { key: "review", agent: critic },
    ],
    [
      ["research", "draft"],
      ["research", "fact_check"],
      ["draft", "review"],
      ["fact_check", "review"],
    ],
  );

  // 1. The list shows the workflows; open one.
  await page.goto("/workflows");
  await expect(page.getByRole("link", { name: /Triage/ })).toBeVisible();
  const listed = page.getByRole("link", { name: /Weekly report/ });
  await expect(listed).toContainText("4 nodes");
  await listed.click();

  // 2. The viewer draws the graph, and the inspector describes what's selected.
  await expect(page).toHaveURL(`/workflows/${weekly.id}`);
  await expect(page.getByRole("heading", { name: "Weekly report" })).toBeVisible();
  for (const key of ["research", "draft", "fact_check", "review"]) {
    await expect(node(page, key)).toBeVisible();
  }
  await expect(edge(page, "fact_check", "review")).toBeAttached();
  await node(page, "draft").click();
  await expect(inspector(page)).toContainText("Writer");
  await expect(inspector(page)).toContainText("After research");
  await edge(page, "research", "draft").dispatchEvent("click");
  await expect(inspector(page)).toContainText("research → draft");

  // 3. Edit: add a node from the picker and connect it; the change is unsaved until saved.
  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page).toHaveURL(`/workflows/${weekly.id}/edit`);
  await node(page, "review").click();
  await addNode(page, "Publisher");
  await expect(node(page, "publisher")).toContainText("Publisher");
  await connect(page, "review", "publisher");
  // Leaving now would lose the change, so it asks; saying no stays.
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toBe("Discard your unsaved changes?");
    void dialog.dismiss();
  });
  await page.getByRole("link", { name: "Agents" }).click();
  await expect(page).toHaveURL(`/workflows/${weekly.id}/edit`);
  await page.getByLabel("Change note").fill("Publish when reviewed");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(`/workflows/${weekly.id}`);
  await expect(page.getByText("Publish when reviewed")).toBeVisible();
  await expect(edge(page, "review", "publisher")).toBeAttached();
  expect(weekly.versions).toHaveLength(2);

  // 4. Start a run of the new version.
  await page.getByRole("button", { name: "Run" }).click();
  await page.getByLabel("Input").fill("Write this week's report");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+\/runs\/\d+$/);
  const status = page.getByTestId("run-status");
  await expect(status).toHaveText("Queued");
  await expect(node(page, "publisher")).toHaveAttribute("data-state", "waiting");
  const run = [...api.runs.values()].at(-1)!;
  expect(run.workflow_version_id).toBe(weekly.versions[1]);

  // 5. The worker moves through the graph a column at a time; the page follows it live.
  const isNode = (key: string, value: string) =>
    expect(node(page, key)).toHaveAttribute("data-state", value);
  const isEdge = (source: string, target: string, value: string) =>
    expect(edge(page, source, target)).toHaveAttribute("data-state", value);

  api.advance(run);
  await expect(status).toHaveText("Running");
  await isNode("research", "running");
  await isNode("draft", "waiting");

  api.advance(run);
  await isNode("research", "succeeded");
  await isNode("draft", "running");
  await isNode("fact_check", "running");
  await isEdge("research", "draft", "live");

  api.advance(run);
  await isNode("review", "running");
  await isEdge("research", "draft", "taken");
  await isEdge("fact_check", "review", "live");
  await isEdge("review", "publisher", "idle");

  api.advance(run);
  await isNode("review", "succeeded");
  await isNode("publisher", "running");
  await isEdge("review", "publisher", "live");

  api.advance(run);
  await expect(status).toHaveText("Succeeded");
  await isNode("publisher", "succeeded");
  await isEdge("review", "publisher", "taken");
  await expect(page.getByRole("region", { name: "Output" })).toContainText("publisher done");

  // 6. Back on the workflow, the finished run is listed; open it again.
  await page.getByRole("heading").getByRole("link", { name: "Weekly report" }).click();
  await expect(page).toHaveURL(`/workflows/${weekly.id}`);
  const latest = page.getByRole("list", { name: "Runs" }).getByRole("listitem").first();
  await expect(latest).toContainText("Write this week's report");
  await expect(latest).toContainText("Succeeded");
  await latest.getByRole("link").click();
  await expect(page).toHaveURL(`/workflows/${weekly.id}/runs/${run.id}`);
  await expect(status).toHaveText("Succeeded");
  await node(page, "publisher").click();
  await expect(inspector(page).getByRole("region", { name: "Step output" })).toContainText(
    "publisher done",
  );
});
