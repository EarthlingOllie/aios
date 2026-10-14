import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";

const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);
const edge = (page: Page, source: string, target: string) =>
  page.locator(`.react-flow__edge[data-id="${source}->${target}"]`);
const inspector = (page: Page) => page.getByRole("complementary", { name: "Inspector" });

async function addNode(page: Page, name: string) {
  await page.getByLabel("Add node").selectOption({ label: name });
  await page.getByRole("button", { name: "Add", exact: true }).click();
}

async function connectInInspector(page: Page, source: string, target: string) {
  await node(page, source).click();
  await inspector(page).getByLabel("Connect to").selectOption(target);
  await inspector(page).getByRole("button", { name: "Connect" }).click();
}

function seed(api: FakeApi) {
  const researcher = api.addAgent("Researcher");
  const writer = api.addAgent("Writer");
  const critic = api.addAgent("Critic");
  const review = api.addWorkflow("Review", [{ key: "critic", agent: critic }]);
  const report = api.addWorkflow(
    "Report",
    [
      { key: "research", agent: researcher },
      { key: "write", agent: writer },
    ],
    [["research", "write"]],
  );
  return { researcher, writer, critic, review, report };
}

test("builds a new workflow from agents and workflows", async ({ page }) => {
  const api = await fakeApi(page);
  const { researcher, review } = seed(api);
  await page.goto("/workflows");
  await page.getByRole("link", { name: "New" }).click();
  await expect(page.getByRole("heading", { name: "New workflow" })).toBeVisible();
  await page.getByLabel("Name").fill("Pipeline");
  await page.getByLabel("Description").fill("Research, then review");

  await addNode(page, "Researcher");
  await addNode(page, "Review");
  await expect(node(page, "researcher")).toContainText("Researcher");
  await expect(node(page, "review")).toContainText("Workflow");
  // A new node is selected, so it can be configured straight away.
  await expect(inspector(page).getByLabel("Key")).toHaveValue("review");

  await connectInInspector(page, "researcher", "review");
  await expect(edge(page, "researcher", "review")).toBeAttached();

  await page.getByRole("button", { name: "Create workflow" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+$/);
  await expect(page.getByRole("heading", { name: "Pipeline" })).toBeVisible();
  await expect(edge(page, "researcher", "review")).toBeAttached();

  const [method, path, body] = api.writes.at(-1)!;
  expect([method, path]).toEqual(["POST", "/api/workflows"]);
  expect(body).toMatchObject({
    name: "Pipeline",
    description: "Research, then review",
    nodes: [
      { key: "researcher", kind: "agent", version_id: researcher.versions[0] },
      { key: "review", kind: "workflow", version_id: review.versions[0] },
    ],
    edges: [{ source: "researcher", target: "review" }],
  });
  expect(Object.keys((body as { layout: object }).layout).sort()).toEqual(["researcher", "review"]);
});

test("edits a workflow's nodes and edges and saves a new version", async ({ page }) => {
  const api = await fakeApi(page);
  const { report, critic } = seed(api);
  await page.goto(`/workflows/${report.id}`);
  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);
  await expect(page.getByLabel("Name")).toHaveValue("Report");
  await expect(
    page.getByText(
      "Changing nodes or edges (including a node's key) creates v2; renaming the workflow or moving nodes doesn't.",
    ),
  ).toBeVisible();

  // Rename a node's key: its edges follow.
  await node(page, "write").click();
  const key = inspector(page).getByLabel("Key");
  await key.fill("draft");
  await key.press("Enter");
  await expect(node(page, "draft")).toBeVisible();
  await expect(edge(page, "research", "draft")).toBeAttached();

  // Point it at another agent.
  await inspector(page).getByLabel("Runs").selectOption({ label: "Critic" });
  await expect(node(page, "draft")).toContainText("Critic");

  // Remove the edge, then the other node with the keyboard.
  await edge(page, "research", "draft").dispatchEvent("click");
  await inspector(page).getByRole("button", { name: "Remove edge" }).click();
  await expect(edge(page, "research", "draft")).toHaveCount(0);
  await node(page, "research").click();
  await page.keyboard.press("Delete");
  await expect(node(page, "research")).toHaveCount(0);

  await page.getByLabel("Change note").fill("Just the critic");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  await expect(node(page, "draft")).toContainText("Critic");
  await expect(page.getByText("Just the critic")).toBeVisible();
  expect(api.current(report)).toMatchObject({
    version: 2,
    nodes: [{ key: "draft", kind: "agent", version_id: critic.versions[0] }],
    edges: [],
  });
});

test("connects nodes by dragging from one handle to another", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await addNode(page, "Critic");
  const from = (await node(page, "write").locator(".react-flow__handle-right").boundingBox())!;
  const to = (await node(page, "critic").locator(".react-flow__handle-left").boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 10 });
  await page.mouse.up();
  await expect(edge(page, "write", "critic")).toBeAttached();
});

test("refuses an edge that would make a cycle, and says why", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "write").click();
  // Only nodes it can lead to are offered.
  const options = inspector(page).getByLabel("Connect to").locator("option");
  await expect(options).toHaveText(["Choose a node…"]);
  await expect(inspector(page)).toContainText("research: That would make a cycle");
});

test("rejects an invalid or duplicate key", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "write").click();
  const key = inspector(page).getByLabel("Key");
  await key.fill("has space");
  await key.press("Enter");
  await expect(inspector(page)).toContainText("Use 1-64 letters");
  await key.fill("research");
  await key.press("Enter");
  await expect(inspector(page)).toContainText("already used");
  await expect(node(page, "write")).toBeVisible();
});

test("takes a newer version of what a node runs", async ({ page }) => {
  const api = await fakeApi(page);
  const { report, writer } = seed(api);
  api.bumpAgent(writer);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "write").click();
  await inspector(page).getByRole("button", { name: "Update to v2" }).click();
  await expect(node(page, "write")).not.toContainText("v2 available");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  expect(api.current(report).nodes[1].version_id).toBe(writer.versions[1]);
});

test("moving nodes saves the layout without a new version", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  const box = (await node(page, "research").boundingBox())!;
  await page.mouse.move(box.x + 20, box.y + 20);
  await page.mouse.down();
  await page.mouse.move(box.x + 20, box.y + 220, { steps: 10 });
  await page.mouse.up();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  expect(report.versions).toHaveLength(1);
  expect(report.layout.research.y).toBeGreaterThan(report.layout.write.y + 100);
});

test("the workflow itself isn't offered as a node", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  const options = page.getByLabel("Add node").locator("option");
  await expect(options.filter({ hasText: "Review" })).toHaveCount(1);
  const labels = await options.allTextContents();
  expect(labels).toContain("Review");
  expect(labels).not.toContain("Report");
});

test("shows the server's reason when a save is refused", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addWorkflow("Taken");
  await page.goto(`/workflows/${report.id}/edit`);
  await page.getByLabel("Name").fill("Taken");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("alert")).toContainText("already have a workflow with that name");
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);
});

test("refuses to overwrite a version saved since the editor loaded", async ({ page }) => {
  const api = await fakeApi(page);
  const { report, critic } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await expect(page.getByText("Editing v1.")).toBeVisible();
  await node(page, "research").click();
  await page.keyboard.press("Delete");
  // Meanwhile, another tab saves v2.
  api.addWorkflowVersion(report, (v) => {
    v.nodes.push({ key: "critic", kind: "agent", version_id: critic.versions[0] });
  });
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "This workflow has changed since you started editing (now v2)",
  );
  // The draft is kept, and v2 isn't overwritten.
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);
  await expect(node(page, "research")).toHaveCount(0);
  expect(report.versions).toHaveLength(2);
  expect(api.writes.at(-1)![2]).toMatchObject({ base_version: 1 });
});

test("cancel discards the changes", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}`);
  await page.getByRole("link", { name: "Edit" }).click();
  await node(page, "research").click();
  await page.keyboard.press("Delete");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  await expect(node(page, "research")).toBeVisible();
  expect(api.writes).toEqual([]);
});

test("won't save while a node's key is invalid", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "write").click();
  const key = inspector(page).getByLabel("Key");
  await key.fill("research");
  await key.press("Enter");
  await expect(inspector(page)).toContainText("already used by another node");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);

  // Once it's fixed, the edit is saved, and only then.
  await key.fill("draft");
  await key.press("Enter");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  expect(api.writes).toHaveLength(1);
  expect(api.current(report).nodes.map((n) => n.key)).toEqual(["research", "draft"]);
});

test("says why there's nothing to add when agents fail to load", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.route("/api/agents", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"boom"}' }),
  );
  await page.goto(`/workflows/${report.id}/edit`);
  await expect(page.getByRole("alert")).toContainText("Couldn't load agents and workflows: boom");
});
