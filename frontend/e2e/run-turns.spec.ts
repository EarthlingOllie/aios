import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** A run's page lists its turns: each with the version it ran on, how it went, how long it took
 * and what it cost. */

test("the run page lists the run's turns", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, "Find sources", 4, {
    status: "failed",
    started_at: "2026-10-14T09:01:00Z",
    finished_at: "2026-10-14T09:01:02Z",
    tool_call_count: 2,
    input_tokens: 1200,
    output_tokens: 34,
  });
  api.addTurn(run, {
    status: "succeeded",
    started_at: "2026-10-14T09:00:00Z",
    finished_at: "2026-10-14T09:00:04.200Z",
    model_calls: 3,
    tool_calls: 2,
    input_tokens: 1000,
    output_tokens: 30,
  });
  api.addTurn(run, {
    status: "failed",
    error: "model overloaded",
    started_at: "2026-10-14T09:01:00Z",
    finished_at: "2026-10-14T09:01:02Z",
    model_calls: 1,
    tool_calls: 0,
    input_tokens: 200,
    output_tokens: 4,
  });
  await page.goto(inOrg(`/agents/${agent.id}/runs/${run.id}`));

  await page.getByRole("tab", { name: "Turns" }).click();
  const turns = page.getByRole("listitem").filter({ hasText: /^Turn / });
  await expect(turns).toHaveCount(2);
  await expect(turns.nth(0)).toContainText("Turn 1");
  await expect(turns.nth(0)).toContainText("Succeeded");
  await expect(turns.nth(0)).toContainText("v1");
  await expect(turns.nth(0)).toContainText("4.2s · 3 model calls · 2 tool calls · 1,030 tokens");
  await expect(turns.nth(1)).toContainText("Failed");
  await expect(turns.nth(1)).toContainText("2.0s · 1 model call · 0 tool calls · 204 tokens");
  await expect(turns.nth(1)).toContainText("model overloaded");
  // The run's own line is its turns' totals, as before.
  await expect(page.getByTestId("run-counters")).toHaveText("2.0s · 2 tool calls · 1,234 tokens");
});

test("a run with no turns yet says so", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const run = api.addAgentRun(agent, null, 0);
  await page.goto(inOrg(`/agents/${agent.id}/runs/${run.id}`));

  await page.getByRole("tab", { name: "Turns" }).click();
  await expect(page.getByText("No turns yet.")).toBeVisible();
});
