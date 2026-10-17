import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** Agent runs show how they went and what they cost: status, duration, tool calls and tokens.
 * Runs from before this was kept say so, rather than showing zero. */

const started_at = "2026-10-14T09:00:00Z";

test("the run list shows each run's status, duration, tool calls and tokens", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  api.addAgentRun(agent, "Old chat", 4);
  api.addAgentRun(agent, "Find sources", 6, {
    status: "succeeded",
    started_at,
    finished_at: "2026-10-14T09:00:04.200Z",
    tool_call_count: 2,
    input_tokens: 1200,
    output_tokens: 34,
  });
  api.addAgentRun(agent, "Broken search", 2, {
    status: "failed",
    started_at,
    finished_at: "2026-10-14T09:03:05Z",
    tool_call_count: 1,
    input_tokens: 300,
    output_tokens: 5,
  });
  api.addAgentRun(agent, "Still going", 1, { status: "running", started_at });
  await page.goto(`/agents/${agent.id}?tab=runs`);

  const row = (title: string) => page.getByRole("link", { name: new RegExp(title) });
  await expect(row("Find sources")).toContainText("Succeeded");
  await expect(row("Find sources")).toContainText("4.2s · 2 tool calls · 1.2k tokens");
  await expect(row("Broken search")).toContainText("Failed");
  await expect(row("Broken search")).toContainText("3m 5s · 1 tool call · 305 tokens");
  await expect(row("Still going")).toContainText("Running");
  await expect(row("Still going")).toContainText("0 tool calls · 0 tokens");
  await expect(row("Old chat")).toContainText("Not tracked");
  await expect(row("Old chat")).not.toContainText("tokens");
});

test("the run page shows the run's counters, and keeps its totals from the messages", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, "Find sources", 6, {
    status: "succeeded",
    started_at,
    finished_at: "2026-10-14T09:00:04.200Z",
    tool_call_count: 2,
    input_tokens: 1200,
    output_tokens: 34,
  });
  await page.goto(`/agents/${agent.id}/runs/${run.id}`);

  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");
  await expect(page.getByTestId("run-counters")).toHaveText("4.2s · 2 tool calls · 1,234 tokens");
  // The event log's totals come from the stored messages, as they always have.
  await page.getByRole("tab", { name: "Event log" }).click();
  await expect(page.getByText("Nothing has happened in this run yet.")).toBeVisible();
});

test("a run from before counters were kept is shown as not tracked", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, "Old chat", 4);
  await page.goto(`/agents/${agent.id}/runs/${run.id}`);

  await expect(page.getByTestId("run-counters")).toHaveText("Not tracked");
  await expect(page.getByTestId("run-status")).toHaveCount(0);
});
