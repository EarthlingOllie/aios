import { expect, test, type Locator, type Page } from "@playwright/test";

import { fakeApi, type AgentDependentsRow, type FakeApi } from "./fake-api";

/**
 * Deleting an agent first checks what depends on it. With nothing, it's a simple confirm;
 * otherwise the dialog lists the runs still going (with the requests they wait on), the
 * workflows that run it and the schedules that would start them, each a link that opens in a new
 * tab, and Delete stays disabled until each has a resolution: cancel the runs, and deactivate
 * each workflow (or edit it to stop running the agent, which takes it off the list).
 */

const dialog = (page: Page) => page.getByRole("dialog", { name: /Delete/ });
const deleteButton = (page: Page) => dialog(page).getByRole("button", { name: "Delete agent" });
const choose = (group: Locator, name: "Deactivate" | "Edit workflow") =>
  group.getByRole("button", { name }).click();

const trigger = (id: number, description: string, enabled = true) => ({
  id,
  enabled,
  cron: "0 2 * * *",
  description,
  timezone: "UTC",
  next_run_at: null,
});

function seed(api: FakeApi) {
  const agent = api.addAgent("Builder");
  const run = api.addAgentRun(agent, "Make a contacts table");
  const dependents: AgentDependentsRow = {
    agent_runs: [
      {
        id: run.id,
        title: run.title,
        waiting: true,
        running: false,
        trigger_id: null,
        updated_at: "2026-10-14T09:00:00Z",
        requests: [{ id: "req-chat", summary: "Create table data.contacts", status: "pending" }],
      },
    ],
    workflow_runs: [
      {
        id: 501,
        workflow_id: 51,
        workflow_name: "Nightly build",
        status: "waiting",
        created_at: "2026-10-14T09:00:00Z",
        waiting_nodes: ["review/build"],
        requests: [{ id: "req-flow", summary: "Create table data.places", status: "pending" }],
      },
    ],
    workflows: [
      { id: 51, name: "Nightly build", version: 3, direct: true, triggers: [trigger(71, "At 02:00")] },
      { id: 52, name: "Release", version: 1, direct: false, triggers: [] },
    ],
    triggers: [trigger(81, "Every hour")],
  };
  api.agentDependents.set(agent.id, dependents);
  return { agent, run, dependents };
}

async function open(page: Page, agentId: number) {
  await page.goto(`/agents/${agentId}`);
  await page.getByRole("button", { name: "Delete agent" }).click();
  await expect(dialog(page)).toBeVisible();
}

test("an agent nothing depends on is deleted with a simple confirm", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Idle");
  await open(page, agent.id);

  await expect(dialog(page)).toContainText("Delete the agent “Idle”, its versions and all of its runs?");
  await expect(dialog(page).getByRole("checkbox")).toHaveCount(0);
  await deleteButton(page).click();

  await expect(page).toHaveURL(/\/agents$/);
  expect(api.agents.has(agent.id)).toBe(false);
  await expect(page.getByRole("link", { name: /Idle/ })).toHaveCount(0);
});

test("cancel closes the dialog and deletes nothing", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Idle");
  await open(page, agent.id);
  await dialog(page).getByRole("button", { name: "Cancel" }).click();
  await expect(dialog(page)).toHaveCount(0);

  await page.getByRole("button", { name: "Delete agent" }).click();
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toHaveCount(0);
  expect(api.agents.has(agent.id)).toBe(true);
});

test("each thing that depends on it links to where it's seen or managed, in a new tab", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { agent, run } = seed(api);
  await open(page, agent.id);
  const d = dialog(page);
  const runs = d.getByRole("list", { name: "Runs still going" });

  const links: [Locator, string][] = [
    // The conversation, and the request it waits on.
    [runs.getByRole("link", { name: "Make a contacts table" }), `/agents/${agent.id}/runs/${run.id}`],
    [
      runs.getByRole("link", { name: "Schema change: Create table data.contacts" }),
      "/data?tab=changes&request=req-chat",
    ],
    // The workflow run's page, which shows its waiting node, and its request.
    [runs.getByRole("link", { name: "Nightly build · run 501" }), "/workflows/51/runs/501"],
    [
      runs.getByRole("link", { name: "Schema change: Create table data.places" }),
      "/data?tab=changes&request=req-flow",
    ],
    // Each workflow's editor, and its schedules where they're managed: on its Schedule tab.
    [d.getByRole("group", { name: "Nightly build" }).getByRole("link", { name: "Nightly build" }), "/workflows/51/edit"],
    [d.getByRole("group", { name: "Release" }).getByRole("link", { name: "Release" }), "/workflows/52/edit"],
    [d.getByRole("link", { name: "Schedule: At 02:00 (UTC)" }), "/workflows/51?tab=schedule"],
    // The agent's own schedules, on its Schedule tab.
    [d.getByRole("link", { name: "Schedule: Every hour (UTC)" }), `/agents/${agent.id}?tab=schedule`],
  ];
  for (const [link, href] of links) {
    await expect(link).toHaveAttribute("href", href);
    await expect(link).toHaveAttribute("target", "_blank");
  }
  await expect(runs).toContainText("Waiting at review/build");
  await expect(d.getByText("Waiting for a decision")).toBeVisible();

  // Following one opens a new tab and leaves the dialog, and what was chosen, as it was.
  await d.getByRole("checkbox", { name: "Cancel these runs" }).check();
  const [tab] = await Promise.all([
    page.context().waitForEvent("page"),
    runs.getByRole("link", { name: "Nightly build · run 501" }).click(),
  ]);
  await tab.waitForLoadState();
  expect(new URL(tab.url()).pathname).toBe("/workflows/51/runs/501");
  await expect(dialog(page)).toBeVisible();
  await expect(d.getByRole("checkbox", { name: "Cancel these runs" })).toBeChecked();
});

test("deletes once each is resolved: the runs cancelled, each workflow deactivated", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { agent, run } = seed(api);
  await open(page, agent.id);
  const d = dialog(page);
  const nightly = d.getByRole("group", { name: "Nightly build" });
  const release = d.getByRole("group", { name: "Release" });
  await expect(nightly).toContainText("1 schedule");
  await expect(release).toContainText("through a nested workflow");

  await expect(deleteButton(page)).toBeDisabled();
  await d.getByRole("checkbox", { name: "Cancel these runs" }).check();
  await expect(deleteButton(page)).toBeDisabled();
  await choose(nightly, "Deactivate");
  await expect(nightly.getByRole("button", { name: "Deactivate" })).toHaveAttribute("aria-pressed", "true");
  await expect(deleteButton(page)).toBeDisabled();
  await choose(release, "Deactivate");
  await expect(deleteButton(page)).toBeEnabled();

  await deleteButton(page).click();

  await expect(page).toHaveURL(/\/agents$/);
  expect(api.writes.find(([method]) => method === "DELETE")).toEqual([
    "DELETE",
    `/api/agents/${agent.id}`,
    { cancel_agent_runs: [run.id], cancel_workflow_runs: [501], deactivate_workflows: [51, 52] },
  ]);
});

test("a workflow left to edit keeps Delete disabled until it no longer runs the agent", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { agent, dependents } = seed(api);
  await open(page, agent.id);
  const d = dialog(page);
  await d.getByRole("checkbox", { name: "Cancel these runs" }).check();
  await choose(d.getByRole("group", { name: "Nightly build" }), "Deactivate");
  const release = d.getByRole("group", { name: "Release" });
  await choose(release, "Edit workflow");
  await expect(release).toContainText("Edit it to stop running this agent");
  await expect(release.getByRole("link", { name: "Open the editor" })).toHaveAttribute(
    "href",
    "/workflows/52/edit",
  );
  await expect(deleteButton(page)).toBeDisabled();

  // The user edits Release in another tab, and comes back: the list is checked again.
  dependents.workflows = dependents.workflows.filter((w) => w.id !== 52);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));

  await expect(d.getByRole("group", { name: "Release" })).toHaveCount(0);
  await expect(deleteButton(page)).toBeEnabled();
  await deleteButton(page).click();
  await expect(page).toHaveURL(/\/agents$/);
});

test("Check again reloads the list", async ({ page }) => {
  const api = await fakeApi(page);
  const { agent, dependents } = seed(api);
  await open(page, agent.id);
  await expect(dialog(page).getByRole("group", { name: "Release" })).toBeVisible();

  dependents.workflows = dependents.workflows.filter((w) => w.id !== 52);
  await dialog(page).getByRole("button", { name: "Check again" }).click();

  await expect(dialog(page).getByRole("group", { name: "Release" })).toHaveCount(0);
});

test("with a long list, Delete and Cancel stay in view while it scrolls", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 600 });
  const api = await fakeApi(page);
  const { agent, dependents } = seed(api);
  for (let i = 0; i < 8; i++) {
    dependents.workflows.push({ id: 60 + i, name: `Flow ${i}`, version: 1, direct: true, triggers: [] });
  }
  await open(page, agent.id);

  await expect(dialog(page).getByRole("group", { name: "Flow 7" })).toBeAttached();
  await expect(deleteButton(page)).toBeInViewport();
  await expect(dialog(page).getByRole("button", { name: "Cancel" })).toBeInViewport();
});

test("deactivating says what it turns off: the schedules, if it has any", async ({ page }) => {
  const api = await fakeApi(page);
  const { agent } = seed(api);
  await open(page, agent.id);
  const nightly = dialog(page).getByRole("group", { name: "Nightly build" });
  const release = dialog(page).getByRole("group", { name: "Release" });

  await choose(nightly, "Deactivate");
  await choose(release, "Deactivate");

  await expect(nightly).toContainText("Its schedule is turned off");
  await expect(release).not.toContainText("schedule is turned off");
  await expect(release).not.toContainText("schedules are turned off");
  await expect(release).toContainText(/no run of it can start/i);
});

test("a refused delete shows why and what depends on it now, to choose again", async ({ page }) => {
  const api = await fakeApi(page);
  const { agent, dependents } = seed(api);
  dependents.workflows = [];
  await open(page, agent.id);
  const d = dialog(page);
  await d.getByRole("checkbox", { name: "Cancel these runs" }).check();

  // Another run starts after the dialog loaded.
  dependents.workflow_runs.push({
    id: 502,
    workflow_id: 51,
    workflow_name: "Nightly build",
    status: "running",
    created_at: "2026-10-14T09:00:00Z",
    waiting_nodes: [],
    requests: [],
  });
  await deleteButton(page).click();

  await expect(d.getByRole("alert")).toContainText("still in use");
  await expect(d.getByRole("link", { name: "Nightly build · run 502" })).toBeVisible();
  // The list changed, so the runs must be agreed to again.
  await expect(d.getByRole("checkbox", { name: "Cancel these runs" })).not.toBeChecked();
  await expect(deleteButton(page)).toBeDisabled();
  expect(api.agents.has(agent.id)).toBe(true);
});
