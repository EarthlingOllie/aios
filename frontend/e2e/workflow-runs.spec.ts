import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";

const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);
const edge = (page: Page, source: string, target: string) =>
  page.locator(`.react-flow__edge[data-id="${source}->${target}"]`);
const inspector = (page: Page) => page.getByRole("complementary", { name: "Inspector" });

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
      { key: "review", workflow: review },
    ],
    [
      ["research", "write"],
      ["write", "review"],
    ],
  );
  return { researcher, writer, critic, review, report };
}

test("starts a run from the workflow page", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  await page.goto(`/workflows/${report.id}`);
  await page.getByRole("button", { name: "Run" }).click();
  await page.getByLabel("Input").fill("Summarise this week's tickets");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+\/runs\/\d+$/);
  await expect(page.getByText("Summarise this week's tickets")).toBeVisible();
  await expect(page.getByTestId("run-status")).toHaveText("Queued");
  // Nothing has started yet, so every node is waiting.
  await expect(node(page, "research")).toHaveAttribute("data-state", "waiting");
});

test("shows where a live run is, and follows it to the end", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  const run = api.addRun(report, {
    steps: [
      {
        path: ["research"],
        status: "succeeded",
        output: "Three sources",
        agent_id: 1,
        agent_run_id: 9,
      },
      { path: ["write"], status: "running", input: "Three sources" },
    ],
  });
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);

  await expect(page.getByTestId("run-status")).toHaveText("Running");
  await expect(node(page, "research")).toHaveAttribute("data-state", "succeeded");
  await expect(node(page, "write")).toHaveAttribute("data-state", "running");
  await expect(node(page, "review")).toHaveAttribute("data-state", "waiting");
  // The path so far is highlighted, and the edge into the running node is animated.
  await expect(edge(page, "research", "write")).toHaveClass(/animated/);
  await expect(edge(page, "research", "write")).toHaveAttribute("data-state", "live");
  await expect(edge(page, "write", "review")).toHaveAttribute("data-state", "idle");

  // The worker moves on; the page follows without a reload.
  const write = run.steps[1];
  Object.assign(write, {
    status: "succeeded",
    output: "A draft",
    finished_at: "2026-10-14T09:01:00Z",
  });
  api.addStep(run, { path: ["review"], kind: "workflow", input: "A draft" });
  api.addStep(run, { path: ["review", "critic"], input: "A draft" });
  await expect(node(page, "review")).toHaveAttribute("data-state", "running");
  await expect(edge(page, "write", "review")).toHaveAttribute("data-state", "live");
  await expect(edge(page, "research", "write")).toHaveAttribute("data-state", "taken");

  for (const s of run.steps.slice(2)) Object.assign(s, { status: "succeeded", output: "Approved" });
  Object.assign(run, {
    status: "succeeded",
    output: "Approved",
    finished_at: "2026-10-14T09:02:00Z",
  });
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");
  await expect(page.getByRole("region", { name: "Output" })).toContainText("Approved");
  await expect(node(page, "review")).toHaveAttribute("data-state", "succeeded");
});

test("a node's step shows its input, output and conversation", async ({ page }) => {
  const api = await fakeApi(page);
  const { report, researcher } = seed(api);
  const run = api.addRun(report, {
    status: "succeeded",
    output: "Done",
    steps: [
      {
        path: ["research"],
        status: "succeeded",
        input: "Find sources",
        output: "Three sources",
        agent_id: researcher.id,
        agent_run_id: 42,
      },
    ],
  });
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await node(page, "research").click();
  const panel = inspector(page);
  await expect(panel).toContainText("Succeeded");
  await expect(panel.getByRole("region", { name: "Step input" })).toContainText("Find sources");
  await expect(panel.getByRole("region", { name: "Step output" })).toContainText("Three sources");
  await expect(panel.getByRole("link", { name: "Open the conversation" })).toHaveAttribute(
    "href",
    `/agents/${researcher.id}/runs/42`,
  );
  // A node that never ran says so.
  await node(page, "write").click();
  await expect(panel).toContainText("Didn't run");
});

test("a failed run shows the error, where it happened, and what didn't run", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  const run = api.addRun(report, {
    status: "failed",
    error: "write: The model is overloaded",
    steps: [
      { path: ["research"], status: "succeeded", output: "Sources" },
      { path: ["write"], status: "failed", error: "The model is overloaded" },
    ],
  });
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await expect(page.getByTestId("run-status")).toHaveText("Failed");
  await expect(page.getByRole("alert")).toContainText("write: The model is overloaded");
  await expect(node(page, "write")).toHaveAttribute("data-state", "failed");
  await expect(node(page, "review")).toHaveAttribute("data-state", "not-run");
  await node(page, "write").click();
  await expect(inspector(page)).toContainText("The model is overloaded");
});

test("opens a subworkflow's graph with its own steps", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  const run = api.addRun(report, {
    steps: [
      { path: ["research"], status: "succeeded" },
      { path: ["write"], status: "succeeded" },
      { path: ["review"], kind: "workflow", status: "running" },
      { path: ["review", "critic"], status: "running" },
    ],
  });
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await node(page, "review").click();
  await inspector(page).getByRole("button", { name: "Open subworkflow" }).click();
  const crumbs = page.getByRole("navigation", { name: "Subworkflow path" });
  await expect(crumbs).toContainText("Report");
  await expect(crumbs).toContainText("review");
  await expect(node(page, "critic")).toHaveAttribute("data-state", "running");
  await expect(node(page, "research")).toHaveCount(0);
  await crumbs.getByRole("button", { name: "Report" }).click();
  await expect(node(page, "research")).toBeVisible();
});

test("lists a workflow's runs and deletes one", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addRun(report, { status: "succeeded", input: "First run", output: "ok" });
  const second = api.addRun(report, { status: "failed", input: "Second run", error: "x" });
  await page.goto(`/workflows/${report.id}`);
  const runs = page.getByRole("list", { name: "Runs" });
  await expect(runs.getByRole("listitem")).toHaveCount(2);
  await expect(runs.getByRole("listitem").first()).toContainText("Second run");
  await expect(runs.getByRole("listitem").first()).toContainText("Failed");
  await runs.getByRole("link", { name: /Second run/ }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/runs/${second.id}`);
  // The workflows list shows nothing about runs, so deleting one leaves it be.
  const listReads: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "GET" && new URL(r.url()).pathname === "/api/workflows")
      listReads.push(r.url());
  });
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete run" }).click();
  await expect(page).toHaveURL(`/workflows/${report.id}`);
  await expect(page.getByRole("list", { name: "Runs" }).getByRole("listitem")).toHaveCount(1);
  expect(listReads).toEqual([]);
});
