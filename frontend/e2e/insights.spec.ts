import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Insights: every member sees the panels; the range and grouping are kept in the URL; turns
 * from before counts were kept read as not tracked; costs are estimates in pounds; usage by
 * person is admins' only until an admin shows it to everyone; failures link to the latest
 * failed run; source health shows once there's a source beyond Uploads.
 */

const panel = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

const helperUsage = {
  key: 1,
  name: "Helper",
  turns: 3,
  not_tracked: 1,
  model_calls: 6,
  tool_calls: 4,
  input_tokens: 1200,
  output_tokens: 80,
  cost: 0.0032,
  unpriced: 0,
};

for (const role of ["admin", "editor", "participant"] as const) {
  test(`${role === "admin" ? "an" : "a"} ${role} sees every panel`, async ({ page }) => {
    const api = await fakeApi(page);
    api.actAs(role);
    await page.goto(inOrg("/insights"));

    await expect(page.getByRole("heading", { name: "Insights", level: 1 })).toBeVisible();
    for (const name of ["Activity", "Usage and cost", "Workflows", "Approvals", "Chat activity", "Storage"]) {
      await expect(panel(page, name)).toBeVisible();
    }
    await expect(page.getByText("Coming soon.")).toHaveCount(0);
    // Only Uploads: no source health.
    await expect(panel(page, "Source health")).toHaveCount(0);
  });
}

test("the range and grouping survive a reload and a shared link", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/insights"));
  await page.getByRole("combobox", { name: "Range" }).selectOption("7d");
  await page.getByRole("combobox", { name: "Group by" }).selectOption("cause");
  await expect(page).toHaveURL(/\/insights\?range=7d&group=cause$/);

  await page.reload();
  await expect(page.getByRole("combobox", { name: "Range" })).toHaveValue("7d");
  await expect(page.getByRole("combobox", { name: "Group by" })).toHaveValue("cause");
  await expect.poll(() => api.insightsReads.filter((r) => r.startsWith("usage?")).at(-1)).toContain("group=cause");

  // A link opened afresh shows the same view.
  const shared = await page.context().newPage();
  const again = await fakeApi(shared);
  await shared.goto(inOrg("/insights?range=90d&group=workflow"));
  await expect(shared.getByRole("combobox", { name: "Range" })).toHaveValue("90d");
  await expect(shared.getByRole("combobox", { name: "Group by" })).toHaveValue("workflow");
  await expect.poll(() => again.insightsReads.find((r) => r.startsWith("activity?"))).toContain("group=workflow");
});

test("turns from before counts were kept read as not tracked, and costs as estimates", async ({ page }) => {
  const api = await fakeApi(page);
  api.insights.usage.agent = {
    rows: [
      helperUsage,
      { ...helperUsage, key: 2, name: "Old timer", turns: 2, not_tracked: 2, model_calls: null, tool_calls: null, input_tokens: null, output_tokens: null, cost: null },
    ],
    top_agents: [{ key: 1, name: "Helper", turns: 3 }],
    top_tools: [{ tool: "query_data", calls: 4 }],
  };
  await page.goto(inOrg("/insights"));

  const usage = panel(page, "Usage and cost");
  await expect(usage).toContainText("Costs are an estimate, not a bill.");
  const old = usage.getByRole("row", { name: /Old timer/ });
  await expect(old.getByRole("cell")).toHaveText(["Old timer", "2", "Not tracked", "Not tracked", "Not tracked", "No price"]);
  const helper = usage.getByRole("row", { name: /Helper/ });
  await expect(helper.getByRole("cell")).toHaveText(["Helper", "3", "1,200", "80", "4", "£0.0032"]);
  await expect(usage).toContainText("3 turns are from before counts were kept");
  await expect(usage.getByRole("list", { name: "Most used tools" })).toContainText("query_data");
});

test("usage by person is admins' only until an admin shows it to everyone", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  await page.goto(inOrg("/insights?group=person"));
  await expect(panel(page, "Usage and cost").getByRole("alert")).toHaveText(
    "Usage by person is shown to admins only. An admin can show it to everyone in the organisation's settings.",
  );
  await expect(page.getByRole("checkbox", { name: /Show usage by person/ })).toHaveCount(0);

  api.actAs("admin");
  await page.goto(inOrg("/insights?group=person"));
  await expect(panel(page, "Usage and cost").getByRole("alert")).toHaveCount(0);
  await page.getByRole("checkbox", { name: "Show usage by person to everyone, not only admins" }).click();
  await expect(page.getByRole("checkbox", { name: /Show usage by person/ })).toBeChecked();
  expect(api.writes).toContainEqual(["PUT", orgApi("/organisation/insights-people"), { visible: true }]);

  api.actAs("editor");
  await page.goto(inOrg("/insights?group=person"));
  await expect(panel(page, "Usage and cost")).toBeVisible();
  await expect(panel(page, "Usage and cost").getByRole("alert")).toHaveCount(0);
});

test("failures link to the failing node's latest failed run, and expired approvals count apart", async ({ page }) => {
  const api = await fakeApi(page);
  api.insights.workflows.failures = [
    { workflow_id: 7, name: "Fetch", node: "fetch", kind: "agent", failures: 2, latest_at: "2026-11-10T09:00:00Z", latest_run_id: 41 },
  ];
  api.insights.approvals.kinds = [
    {
      kind: "workflow_step",
      total: 4,
      approved: 1,
      rejected: 1,
      expired: 1,
      cancelled: 0,
      pending: 1,
      mean_wait_seconds: 200,
      max_wait_seconds: 300,
    },
  ];
  await page.goto(inOrg("/insights"));

  const failures = panel(page, "Workflows").getByRole("table", { name: "Where runs fail" });
  await expect(failures.getByRole("row", { name: /Fetch · fetch/ }).getByRole("cell").nth(1)).toHaveText("2");
  await expect(failures.getByRole("link", { name: "Latest error at Fetch · fetch" })).toHaveAttribute(
    "href",
    /\/workflows\/7\/runs\/41$/,
  );
  const approvals = panel(page, "Approvals").getByRole("row", { name: /Workflow approval steps/ });
  await expect(approvals.getByRole("cell")).toHaveText(["Workflow approval steps", "4", "1", "1", "50%", "1", "1", "3 min 20 s", "5 min"]);
});

test("storage shows usage against the caps, and source health once there's a source", async ({ page }) => {
  const api = await fakeApi(page);
  const MB = 1024 ** 2;
  api.insights.storage = {
    ...api.insights.storage,
    db_bytes: 12 * MB,
    files_bytes: 300 * MB,
    measured_at: "2026-11-12T08:00:00Z",
    samples: [
      { measured_at: "2026-11-01T08:00:00Z", db_bytes: 10 * MB, files_bytes: 200 * MB },
      { measured_at: "2026-11-12T08:00:00Z", db_bytes: 12 * MB, files_bytes: 300 * MB },
    ],
  };
  api.insights.sources = {
    shown: true,
    sources: [
      {
        id: 5,
        kind: "connector",
        name: "Team Slack",
        status: "failing",
        health: "failing",
        provider: "slack",
        interval_seconds: 900,
        document_count: 0,
        item_count: 10,
        last_sync: null,
        next_sync_at: null,
        created_at: "2026-11-01T08:00:00Z",
      },
    ],
  };
  await page.goto(inOrg("/insights"));

  const storage = panel(page, "Storage");
  await expect(storage).toContainText("12 MB of 1.0 GB");
  await expect(storage).toContainText("300 MB of 5.0 GB");
  await expect(storage.getByRole("img", { name: "Documents over time" })).toBeVisible();
  const health = panel(page, "Source health").getByRole("row", { name: /Team Slack/ });
  await expect(health.getByRole("cell")).toHaveText(["Team Slack", "Failing", "Never"]);
});

// --- Charts ----------------------------------------------------------------------------------

const charts = (page: Page) => panel(page, "Charts");
const fishChart = {
  title: "Fish by weight",
  sql: "SELECT name, weight_kg FROM data.fish ORDER BY name",
  spec: {
    mark: "bar",
    encoding: { x: { field: "name", type: "nominal" }, y: { field: "weight_kg", type: "quantitative" } },
  },
  data: [
    { name: "carp", weight_kg: 7.5 },
    { name: "perch", weight_kg: 0.8 },
  ],
};

async function askFor(page: Page, question: string) {
  await charts(page).getByRole("textbox", { name: "Ask for a chart" }).fill(question);
  await charts(page).getByRole("button", { name: "Ask" }).click();
}

test("a member asks for a chart in words, sees its SQL, and saves it for everyone", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  api.chartAnswers.push(fishChart);
  await page.goto(inOrg("/insights"));
  await expect(charts(page).getByText("No saved charts yet.")).toBeVisible();

  await askFor(page, "How heavy are the fish?");
  const asked = charts(page).getByRole("group", { name: "Asked chart" });
  await expect(asked.getByRole("heading", { name: "Fish by weight" })).toBeVisible();
  // Drawn by Vega-Lite, as SVG, from the query's rows.
  await expect(asked.locator("figure svg").first()).toBeVisible();
  await expect(asked.locator("figure svg").getByText("carp")).toBeVisible();
  expect(api.chartQuestions).toEqual(["How heavy are the fish?"]);

  // The SQL is always there to see.
  await asked.getByText("SQL", { exact: true }).click();
  await expect(asked.getByText(fishChart.sql)).toBeVisible();

  await asked.getByRole("button", { name: "Save for the organisation" }).click();
  await expect(asked.getByText("Saved to the organisation's charts.")).toBeVisible();
  const saved = charts(page).getByRole("list", { name: "Saved charts" }).getByRole("listitem", { name: "Fish by weight" });
  await expect(saved).toBeVisible();
  expect(api.savedCharts[0]).toMatchObject({ title: "Fish by weight", question: "How heavy are the fish?", sql: fishChart.sql });
});

test("a truncated chart is drawn and marked as cut short", async ({ page }) => {
  const api = await fakeApi(page);
  api.chartAnswers.push({ ...fishChart, truncated: true });
  await page.goto(inOrg("/insights"));
  await askFor(page, "Every fish");
  const asked = charts(page).getByRole("group", { name: "Asked chart" });
  await expect(asked.locator("figure svg").first()).toBeVisible();
  await expect(asked.getByText("Showing the first 2 rows: the result was cut short.")).toBeVisible();
});

test("a chart the agent couldn't fix shows the error with its SQL, and can't be saved", async ({ page }) => {
  const api = await fakeApi(page);
  api.chartAnswers.push({
    ...fishChart,
    data: undefined,
    error: "The mark 'geoshape' isn't allowed in charts; use one of arc, area, bar, line, point, rect, rule, text, tick.",
  });
  await page.goto(inOrg("/insights"));
  await askFor(page, "A map of the fish");
  const asked = charts(page).getByRole("group", { name: "Asked chart" });
  await expect(asked.getByRole("alert")).toContainText("The mark 'geoshape' isn't allowed");
  await expect(asked.getByText(fishChart.sql)).toBeVisible();
  await expect(asked.locator("figure")).toHaveCount(0);
  await expect(asked.getByRole("button", { name: "Save for the organisation" })).toHaveCount(0);
});

test("asking too often, or at the budget, says why", async ({ page }) => {
  const api = await fakeApi(page);
  api.chartAnswers.push(
    { status: 429, detail: "You've asked for too many charts. Try again in 60 seconds; nothing was asked." },
    { status: 409, detail: "The organisation has reached its monthly budget of £5.00, so no new work starts until next month, or until an admin raises it." },
  );
  await page.goto(inOrg("/insights"));
  await askFor(page, "Fish?");
  await expect(charts(page).getByRole("alert")).toContainText("too many charts");
  await askFor(page, "Fish again?");
  await expect(charts(page).getByRole("alert")).toContainText("monthly budget");
});

test("a saved chart re-runs its query when shown; one whose query fails says so with its SQL", async ({ page }) => {
  const api = await fakeApi(page);
  const at = "2026-11-12T09:00:00Z";
  const other = { id: 99, name: "Robin Other" };
  api.savedCharts = [
    { id: 2, title: "Broken", question: "", sql: "SELECT * FROM data.gone", spec: fishChart.spec, created_by: other, created_at: at, updated_at: at },
    { id: 1, title: "Fish by weight", question: "How heavy?", sql: fishChart.sql, spec: fishChart.spec, created_by: other, created_at: at, updated_at: at },
  ];
  api.chartRows = { 1: { data: fishChart.data }, 2: { broken: 'relation "data.gone" does not exist' } };
  await page.goto(inOrg("/insights"));

  const list = charts(page).getByRole("list", { name: "Saved charts" });
  const fish = list.getByRole("listitem", { name: "Fish by weight" });
  await expect(fish.locator("figure svg").getByText("perch")).toBeVisible();
  const broken = list.getByRole("listitem", { name: "Broken" });
  await expect(broken.getByText("This chart's query no longer works.")).toBeVisible();
  await expect(broken.getByText("SELECT * FROM data.gone")).toBeVisible();
  await expect(broken.getByText('relation "data.gone" does not exist')).toBeVisible();
  await expect(broken.locator("figure")).toHaveCount(0);
});

test("only a chart's creator or an admin renames or deletes it", async ({ page }) => {
  const api = await fakeApi(page);
  const at = "2026-11-12T09:00:00Z";
  api.savedCharts = [
    { id: 1, title: "Someone else's", question: "", sql: "SELECT 1 AS n", spec: fishChart.spec, created_by: { id: 99, name: "Robin Other" }, created_at: at, updated_at: at },
  ];
  api.chartRows = { 1: { data: [{ n: 1 }] } };
  api.actAs("participant");
  await page.goto(inOrg("/insights"));
  const chart = charts(page).getByRole("listitem", { name: "Someone else's" });
  await expect(chart.getByText("Saved by Robin Other")).toBeVisible();
  await expect(chart.getByRole("button", { name: "Rename" })).toHaveCount(0);
  await expect(chart.getByRole("button", { name: "Delete" })).toHaveCount(0);

  // An admin may.
  api.actAs("admin");
  await page.reload();
  await chart.getByRole("button", { name: "Rename" }).click();
  await chart.getByRole("textbox", { name: "Chart title" }).fill("Ones");
  await chart.getByRole("button", { name: "Save" }).click();
  const renamed = charts(page).getByRole("listitem", { name: "Ones" });
  await expect(renamed).toBeVisible();
  expect(api.savedCharts[0].title).toBe("Ones");

  page.once("dialog", (dialog) => dialog.accept());
  await renamed.getByRole("button", { name: "Delete" }).click();
  await expect(charts(page).getByText("No saved charts yet.")).toBeVisible();
  expect(api.savedCharts).toEqual([]);
});
