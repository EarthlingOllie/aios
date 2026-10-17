import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type AgentDependentsRow, type FakeApi } from "./fake-api";

/**
 * Deleting an agent first checks what depends on it. With nothing, it's a simple confirm;
 * otherwise the dialog lists the runs still going and the workflows that run it, and Delete
 * stays disabled until each has a resolution: cancel the runs, and deactivate each workflow (or
 * edit it to stop running the agent, which takes it off the list).
 */

const dialog = (page: Page) => page.getByRole("dialog", { name: /Delete/ });
const deleteButton = (page: Page) => dialog(page).getByRole("button", { name: "Delete agent" });

function seed(api: FakeApi) {
  const agent = api.addAgent("Builder");
  const run = api.addAgentRun(agent, "Make a contacts table");
  const dependents: AgentDependentsRow = {
    agent_runs: [
      { id: run.id, title: run.title, waiting: true, running: false, trigger_id: null, updated_at: "2026-10-14T09:00:00Z" },
    ],
    workflow_runs: [
      { id: 501, workflow_id: 51, workflow_name: "Nightly build", status: "waiting", created_at: "2026-10-14T09:00:00Z" },
    ],
    workflows: [
      {
        id: 51,
        name: "Nightly build",
        version: 3,
        direct: true,
        triggers: [
          { id: 71, enabled: true, cron: "0 2 * * *", description: "At 02:00", timezone: "UTC", next_run_at: null },
        ],
      },
      { id: 52, name: "Release", version: 1, direct: false, triggers: [] },
    ],
    triggers: [],
  };
  api.agentDependents.set(agent.id, dependents);
  return { agent, run, dependents };
}

test("an agent nothing depends on is deleted with a simple confirm", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Idle");
  await page.goto(`/agents/${agent.id}`);

  await page.getByRole("button", { name: "Delete agent" }).click();

  await expect(dialog(page)).toBeVisible();
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
  await page.goto(`/agents/${agent.id}`);

  await page.getByRole("button", { name: "Delete agent" }).click();
  await dialog(page).getByRole("button", { name: "Cancel" }).click();
  await expect(dialog(page)).toHaveCount(0);

  await page.getByRole("button", { name: "Delete agent" }).click();
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toHaveCount(0);
  expect(api.agents.has(agent.id)).toBe(true);
});

test("lists what depends on it, and deletes once each is resolved", async ({ page }) => {
  const api = await fakeApi(page);
  const { agent, run } = seed(api);
  await page.goto(`/agents/${agent.id}`);

  await page.getByRole("button", { name: "Delete agent" }).click();

  const d = dialog(page);
  await expect(d.getByRole("link", { name: /Make a contacts table/ })).toHaveAttribute(
    "href",
    `/agents/${agent.id}/runs/${run.id}`,
  );
  await expect(d.getByText("Waiting for a decision")).toBeVisible();
  await expect(d.getByRole("link", { name: /Nightly build · run 501/ })).toHaveAttribute(
    "href",
    "/workflows/51/runs/501",
  );
  const nightly = d.getByRole("group", { name: "Nightly build" });
  await expect(nightly).toContainText("1 schedule");
  await nightly.getByRole("radio", { name: "Edit workflow" }).check();
  await expect(nightly.getByRole("link", { name: "Open the editor" })).toHaveAttribute(
    "href",
    "/workflows/51/edit",
  );
  const release = d.getByRole("group", { name: "Release" });
  await expect(release).toContainText("through a nested workflow");

  await expect(deleteButton(page)).toBeDisabled();
  await d.getByRole("checkbox", { name: "Cancel these runs" }).check();
  await expect(deleteButton(page)).toBeDisabled();
  await nightly.getByRole("radio", { name: "Deactivate" }).check();
  await expect(deleteButton(page)).toBeDisabled();
  await release.getByRole("radio", { name: "Deactivate" }).check();
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
  await page.goto(`/agents/${agent.id}`);
  await page.getByRole("button", { name: "Delete agent" }).click();

  const d = dialog(page);
  await d.getByRole("checkbox", { name: "Cancel these runs" }).check();
  await d.getByRole("group", { name: "Nightly build" }).getByRole("radio", { name: "Deactivate" }).check();
  const release = d.getByRole("group", { name: "Release" });
  await release.getByRole("radio", { name: "Edit workflow" }).check();
  await expect(release).toContainText("Edit it to stop running this agent");
  await expect(deleteButton(page)).toBeDisabled();

  // The user edits Release (elsewhere), and checks again.
  dependents.workflows = dependents.workflows.filter((w) => w.id !== 52);
  await d.getByRole("button", { name: "Check again" }).click();

  await expect(d.getByRole("group", { name: "Release" })).toHaveCount(0);
  await expect(deleteButton(page)).toBeEnabled();
  await deleteButton(page).click();
  await expect(page).toHaveURL(/\/agents$/);
});

test("a refused delete shows why and what depends on it now, to choose again", async ({ page }) => {
  const api = await fakeApi(page);
  const { agent, dependents } = seed(api);
  dependents.workflows = [];
  await page.goto(`/agents/${agent.id}`);
  await page.getByRole("button", { name: "Delete agent" }).click();
  const d = dialog(page);
  await d.getByRole("checkbox", { name: "Cancel these runs" }).check();

  // Another run starts waiting after the dialog loaded.
  dependents.workflow_runs.push({
    id: 502,
    workflow_id: 51,
    workflow_name: "Nightly build",
    status: "running",
    created_at: "2026-10-14T09:00:00Z",
  });
  await deleteButton(page).click();

  await expect(d.getByRole("alert")).toContainText("still in use");
  await expect(d.getByRole("link", { name: /Nightly build · run 502/ })).toBeVisible();
  // The list changed, so the runs must be agreed to again.
  await expect(d.getByRole("checkbox", { name: "Cancel these runs" })).not.toBeChecked();
  await expect(deleteButton(page)).toBeDisabled();
  expect(api.agents.has(agent.id)).toBe(true);
});
