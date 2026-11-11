import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi, inOrg, orgApi } from "./fake-api";

const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);

/** A run whose `build` node waits on a schema change request, while `other` has finished. */
function waitingRun(api: FakeApi) {
  const builder = api.addAgent("Builder");
  const after = api.addAgent("After");
  const other = api.addAgent("Other");
  const workflow = api.addWorkflow(
    "Builders",
    [
      { key: "build", agent: builder },
      { key: "after", agent: after },
      { key: "other", agent: other },
    ],
    [["build", "after"]],
  );
  const conversation = api.addAgentRun(builder, "go");
  const request = api.addSchemaChange({ run_id: conversation.id });
  const run = api.addRun(workflow, {
    status: "waiting",
    input: "go",
    waiting_since: "2026-10-14T09:01:00Z",
    steps: [
      {
        path: ["build"],
        status: "waiting",
        input: "go",
        agent_id: builder.id,
        agent_run_id: conversation.id,
        waiting_on_request_ids: [request.id],
      },
      { path: ["other"], status: "succeeded", output: "other(go)" },
    ],
  });
  return { api, builder, workflow, run, request, conversation };
}

test("a waiting run shows its waiting node and the request, approved from the run page", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { workflow, run, request, builder, conversation } = waitingRun(api);
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));

  await expect(page.getByTestId("run-status")).toHaveText("Waiting for you");
  await expect(node(page, "build")).toHaveAttribute("data-state", "waiting");
  await expect(node(page, "other")).toHaveAttribute("data-state", "succeeded");
  // Still to start once it resumes.
  await expect(node(page, "after")).toHaveAttribute("data-state", "pending");

  const waiting = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(waiting).toContainText("build");
  const card = waiting.getByRole("region", { name: `Schema change: ${request.summary}` });
  await expect(card.getByLabel("SQL")).toContainText('CREATE TABLE "data"."contacts"');
  await expect(waiting.getByRole("link", { name: "Open the conversation" })).toHaveAttribute(
    "href",
    inOrg(`/agents/${builder.id}/runs/${conversation.id}`),
  );
  await expect(waiting.getByRole("link", { name: "All schema changes" })).toHaveAttribute(
    "href",
    inOrg("/data/changes"),
  );

  await card.getByRole("button", { name: "Approve" }).click();
  expect(api.writes).toContainEqual(["POST", orgApi(`/schema-changes/${request.id}/approve`), null]);
  await expect(card).toContainText("Applying");

  // The worker applies it and the run carries on; the page follows it to the end.
  api.finishSchemaChange(request.id, { status: "applied" });
  Object.assign(run, { status: "succeeded", output: "done", waiting_since: null });
  Object.assign(run.steps[0], { status: "succeeded", waiting_on_request_ids: [] });
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded", { timeout: 10_000 });
  await expect(waiting).toHaveCount(0);
});

test("rejecting from the run page sends the note", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run, request } = waitingRun(api);
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));

  const card = page.getByRole("region", { name: `Schema change: ${request.summary}` });
  await card.getByRole("button", { name: "Reject…" }).click();
  await card.getByLabel("Note for the agent (optional)").fill("Use a sheet");
  await card.getByRole("button", { name: "Reject", exact: true }).click();

  expect(api.writes).toContainEqual([
    "POST",
    orgApi(`/schema-changes/${request.id}/reject`),
    { note: "Use a sheet" },
  ]);
  await expect(card).toContainText("Rejected: Use a sheet");
});

test("a waiting node's details say what it waits on", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run, request } = waitingRun(api);
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));

  await node(page, "build").click();
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  await expect(inspector).toContainText("Waiting for you");
  await expect(inspector).toContainText(`Waiting for your decision on: ${request.summary}`);
});

test("a waiting run flags the pins it resumes on that are no longer current", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run } = waitingRun(api);
  run.stale_pins = [
    { path: [], kind: "workflow", name: "Builders", pinned_version: 1, current_version: 2 },
    { path: ["build"], kind: "agent", name: "Builder", pinned_version: 1, current_version: 3 },
    { path: ["after"], kind: "agent", name: "After", pinned_version: 1, current_version: null },
  ];
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));

  const pins = page.getByRole("region", { name: "Pinned versions" });
  await expect(pins).toContainText("It resumes on the versions it started with");
  await expect(pins.getByRole("listitem")).toHaveText([
    "This workflow: Builders v1 (now v2)",
    "build: Builder v1 (now v3)",
    "after: After v1 (deleted: this node fails if it runs)",
  ]);
});

test("a waiting run shows no pins notice while every pin is current", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run } = waitingRun(api);
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));
  await expect(page.getByTestId("run-status")).toHaveText("Waiting for you");
  await expect(page.getByRole("region", { name: "Pinned versions" })).toHaveCount(0);
});

test("a node waiting on two requests shows a card for each", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run, request, conversation } = waitingRun(api);
  const second = api.addSchemaChange({ run_id: conversation.id, summary: "Create table data.places" });
  run.steps[0].waiting_on_request_ids = [request.id, second.id];
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));

  const waiting = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(waiting).toContainText("these requests");
  await expect(waiting.getByRole("region", { name: /^Schema change:/ })).toHaveCount(2);
  await expect(
    waiting.getByRole("region", { name: "Schema change: Create table data.places" }),
  ).toBeVisible();
});

test("a request decided elsewhere stops offering approve and reject", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run, request } = waitingRun(api);
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));
  const card = page.getByRole("region", { name: `Schema change: ${request.summary}` });
  await expect(card.getByRole("button", { name: "Approve" })).toBeVisible();

  // Approved on the Data page, in another tab; the run still waits while it's applied.
  Object.assign(request, { status: "approved", decided_at: "2026-10-14T09:05:00Z" });

  await expect(card).toContainText("Applying", { timeout: 10_000 });
  await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
});

test("a run that waited shows how long it ran apart from how long it waited", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run } = waitingRun(api);
  Object.assign(run, {
    status: "succeeded",
    output: "done",
    started_at: "2026-10-14T09:00:00Z",
    finished_at: "2026-10-14T10:00:00Z",
    waiting_since: null,
    waited_seconds: 3540,
  });
  Object.assign(run.steps[0], { status: "succeeded", waiting_on_request_ids: [] });
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));
  await expect(page.getByText(/ran 1m 0s · waited 59m 0s/)).toBeVisible();
});

test("a waiting step says so in the step list rather than counting up", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow, run } = waitingRun(api);
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));
  const build = page.getByRole("listitem").filter({ hasText: /^build/ });
  await expect(build).toContainText("paused");
});
