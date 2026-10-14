import { expect, test, type Locator, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";

const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);
const edge = (page: Page, source: string, target: string) =>
  page.locator(`.react-flow__edge[data-id="${source}->${target}"]`);
const inspector = (page: Page) => page.getByRole("complementary", { name: "Inspector" });

/** Opens a picker of agents and workflows and chooses one. */
async function choose(page: Page, picker: Locator, name: string) {
  await picker.click();
  await page.getByRole("option", { name, exact: true }).click();
}

async function addNode(page: Page, name: string) {
  await choose(page, page.getByLabel("Add node"), name);
  await page.getByRole("button", { name: "Add", exact: true }).click();
}

async function connectInInspector(page: Page, source: string, target: string) {
  await node(page, source).click();
  await choose(page, inspector(page).getByLabel("Connect to"), target);
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
  await choose(page, inspector(page).getByLabel("Runs"), "Critic");
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
  // With research selected, so the new node isn't put right of write to begin with.
  await node(page, "research").click();
  await addNode(page, "Critic");
  const from = (await node(page, "write").locator(".react-flow__handle-right").boundingBox())!;
  const to = (await node(page, "critic").locator(".react-flow__handle-left").boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 10 });
  await page.mouse.up();
  await expect(edge(page, "write", "critic")).toBeAttached();
  // The graph is laid out again, so the new edge runs left to right.
  const x = async (key: string) => (await node(page, key).boundingBox())!.x;
  expect(await x("critic")).toBeGreaterThan(await x("write"));
});

test("lays the graph out again in edge order when edges change", async ({ page }) => {
  const api = await fakeApi(page);
  const { researcher, writer, critic, review } = seed(api);
  api.addAgent("Editor");
  const publisher = api.addAgent("Publisher");
  const weekly = api.addWorkflow(
    "Weekly report",
    [
      { key: "research", agent: researcher },
      { key: "draft", agent: writer },
      { key: "fact_check", agent: critic },
      { key: "review", workflow: review },
      { key: "publish", agent: publisher },
    ],
    [
      ["research", "draft"],
      ["research", "fact_check"],
      ["draft", "review"],
      ["fact_check", "review"],
      ["review", "publish"],
    ],
  );
  await page.goto(`/workflows/${weekly.id}/edit`);
  await addNode(page, "Editor");
  await inspector(page).getByLabel("Key").fill("final_edit");
  await inspector(page).getByLabel("Key").press("Enter");
  await connectInInspector(page, "review", "final_edit");
  await connectInInspector(page, "final_edit", "publish");
  await edge(page, "review", "publish").dispatchEvent("click");
  await inspector(page).getByRole("button", { name: "Remove edge" }).click();

  // Each node is drawn right of what leads to it, and the whole graph is in view.
  const left = async (key: string) => (await node(page, key).boundingBox())!.x;
  const inOrder = async () => {
    expect(await left("review")).toBeGreaterThan(await left("draft"));
    expect(await left("final_edit")).toBeGreaterThan(await left("review"));
    expect(await left("publish")).toBeGreaterThan(await left("final_edit"));
  };
  await inOrder();
  const canvas = (await page.locator(".react-flow").boundingBox())!;
  for (const key of ["research", "final_edit", "publish"]) {
    const box = (await node(page, key).boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(canvas.x);
    expect(box.x + box.width).toBeLessThanOrEqual(canvas.x + canvas.width);
  }

  // What's saved is that layout, so the workflow reads the same once saved.
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(`/workflows/${weekly.id}`);
  await expect(node(page, "final_edit")).toBeVisible();
  await inOrder();
  expect(weekly.layout.final_edit.x).toBeGreaterThan(weekly.layout.review.x);
  expect(weekly.layout.publish.x).toBeGreaterThan(weekly.layout.final_edit.x);
});

test("selects one node at a time, even with Shift held", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "research").click();
  await node(page, "write").click({ modifiers: ["Shift"] });
  // Dragging a box over the canvas doesn't select (or cover) the nodes inside it.
  const pane = (await page.locator(".react-flow__pane").boundingBox())!;
  await page.keyboard.down("Shift");
  await page.mouse.move(pane.x + 2, pane.y + 2);
  await page.mouse.down();
  await page.mouse.move(pane.x + pane.width - 2, pane.y + pane.height - 2, { steps: 5 });
  await page.mouse.up();
  await page.keyboard.up("Shift");
  await page.getByRole("button", { name: "Fit View" }).click();
  await node(page, "research").click();
  await expect(inspector(page).getByLabel("Key")).toHaveValue("research");
  await page.keyboard.press("Delete");
  await expect(node(page, "research")).toHaveCount(0);
  await expect(node(page, "write")).toBeVisible();
});

test("refuses an edge that would make a cycle, and says why", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "write").click();
  // Only nodes it can lead to are offered: none here, so there's nothing to open.
  const picker = inspector(page).getByLabel("Connect to");
  await expect(picker).toHaveText("Nothing to connect to");
  await picker.click();
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(inspector(page).getByRole("button", { name: "Connect" })).toBeDisabled();
  await expect(inspector(page)).toContainText("research: That would make a cycle");
});

test("connects nodes from the inspector with the keyboard", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await addNode(page, "Critic");
  await node(page, "research").click();
  const picker = inspector(page).getByLabel("Connect to");
  await expect(picker).toHaveText("Choose a node…");
  // Not itself, nor write, which it already leads to.
  await picker.click();
  await expect(page.getByRole("listbox").getByRole("option")).toHaveText(["critic"]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("listbox")).toHaveCount(0);

  await picker.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(picker).toHaveText("critic");
  await page.keyboard.press("Tab");
  await expect(inspector(page).getByRole("button", { name: "Connect" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(edge(page, "research", "critic")).toBeAttached();
  // Laid out again: critic is drawn right of what now leads to it.
  await expect
    .poll(async () => (await node(page, "critic").boundingBox())!.x)
    .toBeGreaterThan((await node(page, "research").boundingBox())!.x);
  // Now it leads to every other node, so there's nothing left to offer.
  await expect(picker).toHaveText("Nothing to connect to");
  await expect(inspector(page)).toContainText("critic: research already leads to critic.");
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

test("brings a new node into view", async ({ page }) => {
  const api = await fakeApi(page);
  const { researcher } = seed(api);
  const agents = ["One", "Two", "Three", "Four", "Five"].map((name) => api.addAgent(name));
  const chain = api.addWorkflow(
    "Chain",
    agents.map((agent, i) => ({ key: `n${i}`, agent })),
    agents.slice(1).map((_, i) => [`n${i}`, `n${i + 1}`] as [string, string]),
  );
  await page.setViewportSize({ width: 1440, height: 1500 });
  await page.goto(`/workflows/${chain.id}/edit`);
  await expect(node(page, "n4")).toBeVisible();
  await addNode(page, researcher.name);
  const pane = (await page.locator(".react-flow").boundingBox())!;
  await expect
    .poll(async () => {
      const box = (await node(page, "researcher").boundingBox())!;
      return (
        box.x >= pane.x &&
        box.y >= pane.y &&
        box.x + box.width <= pane.x + pane.width &&
        box.y + box.height <= pane.y + pane.height
      );
    })
    .toBe(true);
});

test("with no agents, says so and links to creating one", async ({ page }) => {
  const api = await fakeApi(page);
  // Other workflows can still be added, but there's no agent to add.
  api.addWorkflow("Review");
  await page.goto("/workflows/new");
  await page.getByLabel("Add node").click();
  await expect(page.getByRole("option")).toHaveText(["Review"]);
  await expect(page.getByText("You have no agents yet.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Create one" })).toHaveAttribute(
    "href",
    "/agents/new",
  );
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
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toContain("Discard");
    void dialog.accept();
  });
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

test("cancel asks before discarding changes, and stays if you say no", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await page.getByLabel("Description").fill("Changed");
  let asked = false;
  page.once("dialog", (dialog) => {
    asked = true;
    void dialog.dismiss();
  });
  await page.getByRole("button", { name: "Cancel" }).click();
  expect(asked).toBe(true);
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);
  await expect(page.getByLabel("Description")).toHaveValue("Changed");
});

test("cancel without changes doesn't ask", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  // Changing something and changing it back leaves nothing to lose.
  await page.getByLabel("Description").fill("Changed");
  await page.getByLabel("Description").fill("");
  let asked = false;
  page.on("dialog", (dialog) => {
    asked = true;
    void dialog.accept();
  });
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  expect(asked).toBe(false);
});

test("leaving the page with changes asks first", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "research").click();
  await page.keyboard.press("Delete");
  const dialog = page.waitForEvent("dialog");
  await page.close({ runBeforeUnload: true });
  expect((await dialog).type()).toBe("beforeunload");
});

test("leaving for another page with changes asks first, and stays if you say no", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await page.getByLabel("Description").fill("Changed");
  const messages: string[] = [];
  page.once("dialog", (dialog) => {
    messages.push(dialog.message());
    void dialog.dismiss();
  });
  await page.getByRole("link", { name: "Agents" }).click();
  await expect.poll(() => messages).toEqual(["Discard your unsaved changes?"]);
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);
  await expect(page.getByLabel("Description")).toHaveValue("Changed");

  // Saying yes leaves, and asks only the once.
  page.on("dialog", (dialog) => {
    messages.push(dialog.message());
    void dialog.accept();
  });
  await page.getByRole("link", { name: "Report" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  expect(messages).toHaveLength(2);
  expect(api.writes).toEqual([]);
});

test("leaving for another page without changes doesn't ask", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  let asked = false;
  page.on("dialog", (dialog) => {
    asked = true;
    void dialog.accept();
  });
  await page.getByRole("link", { name: "Agents" }).click();
  await expect(page).toHaveURL("/agents");
  expect(asked).toBe(false);
});

test("following what a node runs without changes goes there in the same tab", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { report, writer } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await node(page, "write").click();
  let asked = false;
  page.on("dialog", (dialog) => {
    asked = true;
    void dialog.accept();
  });
  let opened = false;
  page.context().on("page", () => (opened = true));
  await inspector(page).getByRole("link", { name: "Writer" }).click();
  await expect(page).toHaveURL(`/agents/${writer.id}`);
  expect(opened).toBe(false);
  expect(asked).toBe(false);
});

test("following what a node runs with changes asks first, and stays if you say no", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { report, writer } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await page.getByLabel("Description").fill("Changed");
  await node(page, "write").click();
  let opened = false;
  page.context().on("page", () => (opened = true));
  const messages: string[] = [];
  page.once("dialog", (dialog) => {
    messages.push(dialog.message());
    void dialog.dismiss();
  });
  const link = inspector(page).getByRole("link", { name: "Writer" });
  await link.click();
  await expect.poll(() => messages).toEqual(["Discard your unsaved changes?"]);
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);
  await expect(page.getByLabel("Description")).toHaveValue("Changed");

  // Saying yes goes there, in the same tab.
  page.on("dialog", (dialog) => {
    messages.push(dialog.message());
    void dialog.accept();
  });
  await link.click();
  await expect(page).toHaveURL(`/agents/${writer.id}`);
  expect(messages).toHaveLength(2);
  expect(opened).toBe(false);
  expect(api.writes).toEqual([]);
});

test("going back with changes asks first", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}`);
  await page.getByRole("link", { name: "Edit" }).click();
  await node(page, "research").click();
  await page.keyboard.press("Delete");
  page.once("dialog", (dialog) => void dialog.dismiss());
  await page.goBack();
  await expect(page).toHaveURL(`/workflows/${report.id}/edit`);
  await expect(node(page, "research")).toHaveCount(0);

  page.once("dialog", (dialog) => void dialog.accept());
  await page.goBack();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  await expect(node(page, "research")).toBeVisible();
});

test("cancel with changes asks only once", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  await page.getByLabel("Description").fill("Changed");
  let asked = 0;
  page.on("dialog", (dialog) => {
    asked++;
    void dialog.accept();
  });
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  expect(asked).toBe(1);
});

test("the workflow itself and ones that run it are shown but can't be added", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  const middle = api.addWorkflow("Middle", [{ key: "report", workflow: report }]);
  api.addWorkflow("Outer", [{ key: "middle", workflow: middle }]);
  await page.goto(`/workflows/${report.id}/edit`);
  const picker = page.getByLabel("Add node");
  await picker.click();
  const options = page.getByRole("listbox").getByRole("option");
  await expect(options).toHaveText([
    "Researcher",
    "Writer",
    "Critic",
    "MiddleRuns this workflow",
    "OuterRuns this workflow",
    "ReportCurrent workflow",
    "Review",
  ]);
  const option = (name: string) => page.getByRole("option", { name, exact: true });
  await expect(option("Report")).toHaveAttribute("aria-disabled", "true");
  await expect(option("Report")).toHaveAccessibleDescription("Current workflow");
  await expect(option("Middle")).toHaveAccessibleDescription("Runs this workflow");
  await expect(option("Review")).not.toHaveAttribute("aria-disabled", "true");

  // Choosing one does nothing; the list stays open for another choice.
  await option("Outer").click({ force: true });
  await expect(page.getByRole("listbox")).toBeVisible();
  await expect(picker).toHaveText("Choose an agent or workflow…");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add", exact: true })).toBeDisabled();
});

test("the picker can be used with the keyboard", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}/edit`);
  const picker = page.getByLabel("Add node");
  await picker.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("listbox")).toBeVisible();
  const active = async () =>
    page.locator(`#${await picker.getAttribute("aria-activedescendant")}`).textContent();
  expect(await active()).toBe("Researcher");
  // The current workflow can be reached, so it's announced, but not chosen.
  await page.keyboard.press("End");
  await page.keyboard.press("ArrowUp");
  expect(await active()).toBe("ReportCurrent workflow");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(picker).toHaveText("Review");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(node(page, "review")).toBeVisible();
});

test("won't update a node to a version that runs this workflow", async ({ page }) => {
  const api = await fakeApi(page);
  const { researcher, review } = seed(api);
  const report = api.addWorkflow("Summary", [
    { key: "research", agent: researcher },
    { key: "review", workflow: review },
  ]);
  // Review's v2 runs a workflow that runs this one.
  const outer = api.addWorkflow("Outer", [{ key: "summary", workflow: report }]);
  api.addWorkflowVersion(review, (v) => {
    v.nodes.push({ key: "outer", kind: "workflow", version_id: outer.versions[0] });
  });
  api.bumpAgent(researcher);
  await page.goto(`/workflows/${report.id}/edit`);

  await node(page, "review").click();
  const update = inspector(page).getByRole("button", { name: "Update to v2" });
  await expect(update).toBeDisabled();
  await expect(update).toHaveAccessibleDescription(
    "Can't update: v2 runs this workflow, which would make a loop.",
  );
  // Other nodes can still be updated.
  await node(page, "research").click();
  await expect(inspector(page).getByRole("button", { name: "Update to v2" })).toBeEnabled();
});
