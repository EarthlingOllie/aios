import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** Agents hand structured work to automations with the `start_workflow` tool: an editor picks
 * the workflows it may start and how long a call waits, and a run an agent started says so. */

const START_WORKFLOW = {
  properties: {
    workflow_ids: {
      type: "array",
      items: { type: "integer" },
      title: "Allowed workflows",
      description: "The workflows the agent may start.",
      "x-picker": "workflows",
    },
    wait_seconds: {
      type: "integer",
      minimum: 0,
      maximum: 600,
      title: "Wait time (seconds)",
    },
  },
};

test("an editor gives an agent start_workflow, picking the workflows it may start", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  api.addTool("start_workflow", "Start workflow", START_WORKFLOW, { workflow_ids: [], wait_seconds: 120 });
  const planner = api.addAgent("Planner");
  const summarise = api.addWorkflow("Summarise");
  api.addWorkflow("Report");
  const old = api.addWorkflow("Old");
  old.archived_at = "2026-11-01T12:00:00Z";
  await page.goto(inOrg(`/agents/${planner.id}/edit`));

  await page.getByRole("checkbox", { name: /Start workflow/ }).check();
  const picker = page.getByRole("group", { name: "Allowed workflows" });
  // The live workflows, not the archived one.
  await expect(picker.getByRole("checkbox")).toHaveCount(2);
  await expect(picker.getByRole("checkbox", { name: "Old" })).toHaveCount(0);
  await picker.getByRole("checkbox", { name: "Summarise" }).check();
  await page.getByRole("spinbutton", { name: "Wait time (seconds)" }).fill("30");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page).toHaveURL(inOrg(`/agents/${planner.id}`));
  const [, , body] = api.writes.at(-1)!;
  expect(body).toMatchObject({
    tools: [{ key: "start_workflow", config: { workflow_ids: [summarise.id], wait_seconds: 30 } }],
  });
});

test("a workflow run an agent started says which, linking to the agent's run", async ({ page }) => {
  const api = await fakeApi(page);
  const planner = api.addAgent("Planner");
  const summarise = api.addWorkflow("Summarise", [{ key: "only", agent: planner }]);
  const run = api.addRun(summarise, {
    status: "succeeded",
    finished_at: "2026-10-15T12:05:00Z",
    started_by_agent: { agent_id: planner.id, name: "Planner", run_id: 77 },
  });
  await page.goto(inOrg(`/workflows/${summarise.id}/runs/${run.id}`));

  const by = page.getByTestId("started-by-agent");
  await expect(by).toHaveText("Started by the agent Planner");
  await expect(by.getByRole("link", { name: "Planner" })).toHaveAttribute(
    "href",
    inOrg(`/agents/${planner.id}/runs/77`),
  );
});
