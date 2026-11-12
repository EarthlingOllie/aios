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
