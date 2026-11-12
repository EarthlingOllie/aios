import { expect, test, type Locator, type Page } from "@playwright/test";

import { fakeApi, type FakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Approvals as one list: the Data page's Approvals tab holds schema changes and workflow
 * approval steps, filterable by kind and status; approvers see an approval step's full prompt
 * and input, and only those who may decide one see its decision buttons. The approval node in
 * the workflow editor, and cancelling a run that waits on one.
 */

const stepCard = (scope: Page | Locator, summary = "Spend 250 on laptops?") =>
  scope.getByRole("region", { name: `Approval: ${summary}` });
const changeCard = (scope: Page | Locator, summary = "Create table data.contacts") =>
  scope.getByRole("region", { name: `Schema change: ${summary}` });
const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);
const inspector = (page: Page) => page.getByRole("complementary", { name: "Inspector" });

/** A run of Spend, waiting at its `approve` node, and the schema change an agent asked for. */
function seed(api: FakeApi) {
  const classify = api.addAgent("Classify");
  const pay = api.addAgent("Pay");
  const workflow = api.addWorkflow(
    "Spend",
    [
      { key: "classify", agent: classify },
      { key: "pay", agent: pay },
    ],
    [["classify", "pay"]],
  );
  const run = api.addRun(workflow, {
    status: "waiting",
    input: "laptops",
    waiting_since: "2026-10-14T09:01:00Z",
    steps: [{ path: ["classify"], status: "succeeded", output: '{"risk":"high","amount":250}' }],
  });
  const approval = api.addApprovalStep(run, ["approve"]);
  const change = api.addSchemaChange();
  return { workflow, run, approval, change };
}

test("the Approvals tab lists both kinds, filtered by kind and status", async ({ page }) => {
  const api = await fakeApi(page);
  const { approval } = seed(api);
  api.addApprovalStep(api.addRun(api.addWorkflow("Old"), { status: "succeeded" }), ["ask"], {
    summary: "Ship it?",
    status: "rejected",
    decided_at: "2026-10-14T09:02:00Z",
    resumed_at: "2026-10-14T09:02:00Z",
    note: "Not yet",
  });
  await page.goto(inOrg("/data/approvals"));

  const pending = page.getByRole("region", { name: "Pending approvals" });
  await expect(stepCard(pending)).toBeVisible();
  await expect(changeCard(pending)).toBeVisible();
  await expect(stepCard(page.getByRole("region", { name: "Approval history" }), "Ship it?")).toContainText(
    "Rejected: Not yet",
  );

  await page.getByLabel("Kind").selectOption({ label: "Workflow approvals" });
  await expect(page).toHaveURL(/kind=workflow_step/);
  await expect(changeCard(page)).toHaveCount(0);
  await expect(stepCard(page)).toBeVisible();

  await page.getByLabel("Status").selectOption({ label: "Rejected" });
  await expect(stepCard(page)).toHaveCount(0);
  await expect(stepCard(page, "Ship it?")).toBeVisible();
  await expect(page.getByRole("region", { name: "Pending approvals" })).toHaveCount(0);

  await page.getByLabel("Status").selectOption({ label: "Approved" });
  await expect(page.getByText("No approvals match.")).toBeVisible();
  expect(approval.status).toBe("pending");
});

test("an approver sees the full prompt and input, and the run it holds, and approves it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { approval, workflow, run } = seed(api);
  await page.goto(inOrg(`/data/approvals?approval=${approval.id}`));

  const card = stepCard(page);
  await expect(card).toHaveAttribute("aria-current", "true");
  await expect(card.getByLabel("Prompt")).toHaveText("Spend 250 on laptops?\nThe risk is high.");
  await expect(card.getByLabel("Input")).toContainText('"amount": 250');
  await expect(card.getByRole("note", { name: "Who may approve" })).toHaveText(
    "Needs an admin to approve",
  );
  await expect(card.getByRole("link", { name: `From Spend, run #${run.id}` })).toHaveAttribute(
    "href",
    inOrg(`/workflows/${workflow.id}/runs/${run.id}`),
  );

  await card.getByRole("button", { name: "Approve" }).click();

  expect(api.writes).toContainEqual(["POST", orgApi(`/approvals/${approval.id}/approve`), null]);
  await expect(card).toContainText("Approved");
  await expect(card).toContainText("The workflow is carrying on.");
  await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
});

test("rejecting an approval step sends the note", async ({ page }) => {
  const api = await fakeApi(page);
  const { approval } = seed(api);
  await page.goto(inOrg("/data/approvals"));

  await stepCard(page).getByRole("button", { name: "Reject…" }).click();
  await stepCard(page).getByLabel("Note (optional)").fill("Too much");
  await stepCard(page).getByRole("button", { name: "Reject", exact: true }).click();

  expect(api.writes).toContainEqual([
    "POST",
    orgApi(`/approvals/${approval.id}/reject`),
    { note: "Too much" },
  ]);
  await expect(stepCard(page)).toContainText("Rejected: Too much");
});

test("only those who may decide an approval see its decision buttons", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  seed(api);
  await page.goto(inOrg("/data/approvals"));

  // The step needs an admin: the editor sees who may, and no buttons.
  await expect(stepCard(page).getByRole("note", { name: "Who may approve" })).toHaveText(
    "Needs an admin to approve",
  );
  await expect(stepCard(page).getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(stepCard(page).getByRole("button", { name: "Reject…" })).toHaveCount(0);
  // The schema change needs an editor: theirs to decide.
  await expect(changeCard(page).getByRole("button", { name: "Approve" })).toBeVisible();
});

test("a run waiting at an approval node shows it, and cancelling the run cancels it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { workflow, run, approval } = seed(api);
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto(inOrg(`/workflows/${workflow.id}/runs/${run.id}`));

  const waiting = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(waiting).toContainText("waits for:");
  await expect(stepCard(waiting).getByLabel("Prompt")).toContainText("Spend 250 on laptops?");
  await expect(waiting.getByRole("link", { name: "All approvals" })).toHaveAttribute(
    "href",
    inOrg("/data/approvals"),
  );

  await page.getByRole("button", { name: "Cancel run" }).click();

  expect(api.writes).toContainEqual([
    "POST",
    orgApi(`/workflows/${workflow.id}/runs/${run.id}/cancel`),
    null,
  ]);
  await expect(page.getByTestId("run-status")).toHaveText("Failed");
  await expect(page.getByText("Cancelled by")).toBeVisible();
  await expect(waiting).toHaveCount(0);
  expect(approval.status).toBe("cancelled");
});

test("the editor adds an approval node, with its prompt, role and expiry", async ({ page }) => {
  const api = await fakeApi(page);
  const { workflow } = seed(api);
  await page.goto(inOrg(`/workflows/${workflow.id}/edit`));

  await page.locator("#add-node").click();
  await page.getByRole("option", { name: "Approval: a person decides" }).click();
  await page.getByRole("button", { name: "Add", exact: true }).click();

  await expect(node(page, "approval")).toBeVisible();
  await expect(inspector(page)).toContainText("Approval node");
  await inspector(page).getByLabel("Prompt").fill("Pay {{nodes.classify.amount}}?");
  await inspector(page).getByLabel("Prompt").blur();
  await inspector(page).getByLabel("Who decides").selectOption("admin");
  await inspector(page).getByLabel("Expires after (days)").fill("2");
  await inspector(page).getByLabel("Expires after (days)").blur();
  await inspector(page).locator("#node-connect").click();
  await page.getByRole("option", { name: "pay", exact: true }).click();
  await inspector(page).getByRole("button", { name: "Connect" }).click();

  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+$/);
  const [method, path, body] = api.writes.at(-1)!;
  expect([method, path]).toEqual(["PUT", orgApi(`/workflows/${workflow.id}`)]);
  expect(body).toMatchObject({
    nodes: [
      { key: "classify" },
      { key: "pay" },
      {
        key: "approval",
        kind: "approval",
        version_id: null,
        prompt_template: "Pay {{nodes.classify.amount}}?",
        approver_role: "admin",
        expiry_days: 2,
      },
    ],
  });
  expect((body as { edges: unknown[] }).edges).toContainEqual({ source: "approval", target: "pay" });
});

test("an admin sets the organisation's approval expiry", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));

  const section = page.getByRole("region", { name: "Approval expiry" });
  await expect(section).toContainText("Approvals left undecided for 7 days expire");
  await section.getByLabel("Expire after").fill("3");
  await section.getByRole("button", { name: "Set expiry" }).click();

  expect(api.writes).toContainEqual(["PUT", orgApi("/organisation/approval-expiry"), { days: 3 }]);
  await expect(section).toContainText("Approvals left undecided for 3 days expire");
});
