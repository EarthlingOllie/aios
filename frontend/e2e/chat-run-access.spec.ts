import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";

const card = (page: Page) => page.getByRole("log", { name: "Messages" }).getByTestId("run-card");

/** Another user's workflow (with a subworkflow) and agent, members of a chat the tester is in. */
function seed(api: FakeApi) {
  const researcher = api.addAgent("Researcher");
  const critic = api.addAgent("Critic");
  const review = api.addWorkflow("Review", [{ key: "critique", agent: critic }]);
  const report = api.addWorkflow(
    "Report",
    [
      { key: "research", agent: researcher },
      { key: "review", workflow: review },
    ],
    [["research", "review"]],
  );
  for (const row of [researcher, critic, review, report]) api.othersOwn.add(row.id);
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "workflow", report);
  api.addChatTarget(chat, "agent", researcher);
  return { chat, report, researcher };
}

test("a member opens another owner's workflow run from its card, to read only", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, report, researcher } = seed(api);
  const conversation = api.addAgentRun(researcher, "Research", 2);
  conversation.ui_messages = [
    { id: "u", role: "user", parts: [{ type: "text", text: "Find the figures" }] },
    { id: "a", role: "assistant", parts: [{ type: "text", text: "Here they are." }] },
  ];
  const run = api.addRun(report, {
    status: "succeeded",
    output: "All done",
    finished_at: "2026-10-15T12:05:00Z",
    steps: [
      {
        path: ["research"],
        status: "succeeded",
        agent_id: researcher.id,
        agent_run_id: conversation.id,
      },
      { path: ["review"], kind: "workflow", status: "succeeded" },
      { path: ["review", "critique"], status: "succeeded" },
    ],
  });
  api.addChatRunCard(chat, "workflow", report, run.id);

  await page.goto(`/chats/${chat.id}`);
  await card(page).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/runs/${run.id}`);

  // Its name comes with the run; the workflow itself isn't the member's to read.
  await expect(page.getByRole("heading", { name: /Report/ })).toBeVisible();
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");
  await expect(page.getByText("Couldn't load the workflow")).toHaveCount(0);
  await expect(page.getByText(/only its owner/i)).toBeVisible();
  // Nothing to change: no delete.
  await expect(page.getByRole("button", { name: "Delete run" })).toHaveCount(0);

  // A subworkflow opens through the run.
  await page.locator('.react-flow__node[data-id="review"]').click();
  await page.getByRole("button", { name: "Open subworkflow" }).click();
  const breadcrumb = page.getByRole("navigation", { name: "Subworkflow path" });
  await expect(breadcrumb).toContainText("review");
  await expect(page.locator('.react-flow__node[data-id="critique"]')).toBeVisible();
  await breadcrumb.getByRole("button", { name: "Report" }).click();

  // And a node's conversation, read only too.
  await page.locator('.react-flow__node[data-id="research"]').click();
  await page.getByRole("link", { name: "Open the conversation" }).click();
  await expect(page).toHaveURL(`/agents/${researcher.id}/runs/${conversation.id}`);
  await expect(page.getByText("Here they are.")).toBeVisible();
  await expect(page.getByText(/Researcher · v1/)).toBeVisible();
  await expect(page.getByText(/only its owner/i)).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete run" })).toHaveCount(0);
  // Nor are the agent's versions theirs to read.
  await expect(page.getByText(/Researcher · v1/).getByRole("link", { name: "v1" })).toHaveCount(0);

  // It wrote nothing on the way.
  expect(api.writes).toEqual([]);
});

test("the owner's run pages keep their actions", async ({ page }) => {
  const api = await fakeApi(page);
  const researcher = api.addAgent("Researcher");
  const report = api.addWorkflow("Report", [{ key: "research", agent: researcher }]);
  const run = api.addRun(report, { status: "succeeded", finished_at: "2026-10-15T12:05:00Z" });
  const conversation = api.addAgentRun(researcher, "Research");

  await page.goto(`/workflows/${report.id}/runs/${run.id}`);
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");
  await expect(page.getByRole("button", { name: "Delete run" })).toBeVisible();
  await expect(page.getByText(/only its owner/i)).toHaveCount(0);

  await page.goto(`/agents/${researcher.id}/runs/${conversation.id}`);
  await expect(page.getByRole("button", { name: "Delete run" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /message/i })).toBeVisible();
  await expect(page.getByText(/Researcher · v1/).getByRole("link", { name: "v1" })).toBeVisible();
});
