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

test("a live run survives failed re-reads and keeps following", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  const run = api.addRun(report, { steps: [{ path: ["research"], status: "running" }] });
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await expect(node(page, "research")).toHaveAttribute("data-state", "running");

  // A couple of re-reads fail (say, mid-deploy): what was shown stays, with a note.
  let failures = 2;
  await page.route(`**/api/workflows/${report.id}/runs/${run.id}`, (route) =>
    failures-- > 0
      ? route.fulfill({
          status: 502,
          contentType: "application/json",
          body: '{"detail":"Bad Gateway"}',
        })
      : route.fallback(),
  );
  await expect(page.getByText("Couldn't refresh this run")).toBeVisible();
  await expect(node(page, "research")).toHaveAttribute("data-state", "running");

  // Once they get through again, the page carries on following the run.
  Object.assign(run.steps[0], { status: "succeeded", output: "Sources" });
  Object.assign(run, { status: "succeeded", output: "Done", finished_at: "2026-10-14T09:02:00Z" });
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");
  await expect(node(page, "research")).toHaveAttribute("data-state", "succeeded");
  await expect(page.getByText("Couldn't refresh this run")).toBeHidden();
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

test("a run that failed before it started says so, not that it's queued", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  // Its worker died while it was still queued.
  const run = api.addRun(report, {
    status: "failed",
    error: "The run's worker stopped.",
    started_at: null,
    finished_at: "2026-10-14T09:05:00Z",
  });
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await expect(page.getByTestId("run-status")).toHaveText("Failed");
  await expect(page.getByText(/^Failed before it started · /)).toBeVisible();
  await expect(page.getByText(/^Queued /)).toHaveCount(0);
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

test("opens a subworkflow that has since been deleted", async ({ page }) => {
  const api = await fakeApi(page);
  const { review, report } = seed(api);
  const run = api.addRun(report, {
    status: "succeeded",
    steps: [
      { path: ["review"], kind: "workflow", status: "succeeded" },
      { path: ["review", "critic"], status: "succeeded", output: "Looks good" },
    ],
  });
  // As if Report had since dropped it: the run still pins the version it ran.
  review.deleted = true;
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await node(page, "review").click();
  await inspector(page).getByRole("button", { name: "Open subworkflow" }).click();
  await expect(node(page, "critic")).toHaveAttribute("data-state", "succeeded");
  await node(page, "critic").click();
  await expect(inspector(page)).toContainText("Looks good");
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

test("still shows a run whose workflow fails to load, and says why", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  const run = api.addRun(report, { status: "succeeded", input: "Look into it", output: "Done" });
  // Registered after the fake API's route, so it's tried first; the run itself still loads.
  await page.route(`**/api/workflows/${report.id}`, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Workflow store unavailable" }),
    }),
  );
  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await expect(page.getByRole("alert")).toContainText("Workflow store unavailable");
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");
  await expect(page.getByText("Look into it")).toBeVisible();
  await expect(node(page, "research")).toBeVisible();
});

test("the runs list follows a live run until nothing is going", async ({ page }) => {
  const api = await fakeApi(page);
  const { report } = seed(api);
  api.addRun(report, { status: "succeeded", input: "First run", output: "ok" });
  const live = api.addRun(report, { status: "running", input: "Second run" });
  let reads = 0;
  page.on("request", (r) => {
    if (r.method() === "GET" && r.url().endsWith(`/workflows/${report.id}/runs`)) reads++;
  });
  await page.goto(`/workflows/${report.id}`);
  const latest = page.getByRole("list", { name: "Runs" }).getByRole("listitem").first();
  await expect(latest).toContainText("Running");

  // A failed re-read doesn't stop it following the run.
  let failures = 2;
  await page.route(`**/api/workflows/${report.id}/runs`, (route) =>
    failures-- > 0
      ? route.fulfill({
          status: 502,
          contentType: "application/json",
          body: '{"detail":"Bad Gateway"}',
        })
      : route.fallback(),
  );
  await expect.poll(() => failures).toBeLessThan(0);

  // The worker finishes the run; the list catches up without a reload.
  Object.assign(live, { status: "succeeded", output: "done", finished_at: "2026-10-14T09:02:00Z" });
  await expect(latest).toContainText("Succeeded");

  // With nothing left going, it stops asking.
  const settled = reads;
  await page.waitForTimeout(2500);
  expect(reads).toBe(settled);
});
