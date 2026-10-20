import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** Runs are the organisation's: each says which member started it, or that a schedule did, on
 * its list and its page, and every member can open, continue and delete any of them. */

const sam = { id: 2, first_name: "Sam", last_name: "Jones" };

test("an agent's runs say who started each, or that a schedule did", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const scheduled = api.addAgentRun(agent, "Morning digest", 2, { created_by: null, trigger_id: 7 });
  const theirs = api.addAgentRun(agent, "Sam's question", 2, { created_by: sam });
  const mine = api.addAgentRun(agent, "My question", 2);
  await page.goto(`/agents/${agent.id}/runs`);

  const row = (title: string) => page.getByRole("listitem").filter({ hasText: title });
  await expect(row("Morning digest").getByTestId("started-by")).toHaveText("Scheduled");
  await expect(row("Sam's question").getByTestId("started-by")).toHaveText("Started by Sam Jones");
  // No avatars: a user is their name, whole in the tooltip when the line is cut short.
  await expect(row("Sam's question").getByTestId("started-by")).toHaveAttribute("title", "Sam Jones");
  await expect(row("Sam's question").getByTestId("started-by").locator("img")).toHaveCount(0);
  await expect(row("My question").getByTestId("started-by")).toHaveText("Started by you");

  // Another member's run opens like any other: to read, continue and delete.
  await page.goto(`/agents/${agent.id}/runs/${theirs.id}`);
  await expect(page.getByTestId("started-by")).toHaveText("Started by Sam Jones");
  await expect(page.getByRole("textbox", { name: /message/i })).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete run" })).toBeVisible();
  await expect(page.getByText(/Researcher · v1/).getByRole("link", { name: "v1" })).toBeVisible();

  await page.goto(`/agents/${agent.id}/runs/${scheduled.id}`);
  await expect(page.getByTestId("started-by")).toHaveText("Scheduled");
  await page.goto(`/agents/${agent.id}/runs/${mine.id}`);
  await expect(page.getByTestId("started-by")).toHaveText("Started by you");
});

test("a workflow's runs say who started each, or that a schedule did", async ({ page }) => {
  const api = await fakeApi(page);
  const researcher = api.addAgent("Researcher");
  const report = api.addWorkflow("Report", [{ key: "research", agent: researcher }]);
  const done = { status: "succeeded" as const, finished_at: "2026-10-15T12:05:00Z" };
  api.addRun(report, { ...done, input: "Scheduled report", created_by: null, trigger_id: 7 });
  const theirs = api.addRun(report, { ...done, input: "Sam's report", created_by: sam });
  await page.goto(`/workflows/${report.id}/runs`);

  const runs = page.getByRole("list", { name: "Runs" });
  const row = (input: string) => runs.getByRole("listitem").filter({ hasText: input });
  await expect(row("Scheduled report").getByTestId("started-by")).toHaveText("Scheduled");
  await expect(row("Sam's report").getByTestId("started-by")).toHaveText("Started by Sam Jones");

  await page.goto(`/workflows/${report.id}/runs/${theirs.id}`);
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");
  await expect(page.getByTestId("started-by")).toHaveText("Started by Sam Jones");
  await expect(page.getByRole("button", { name: "Delete run" })).toBeVisible();
});
