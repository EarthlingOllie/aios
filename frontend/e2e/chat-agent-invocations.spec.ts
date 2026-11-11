import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi, inOrg } from "./fake-api";

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const message = (page: Page, text: string | RegExp) =>
  messageList(page).getByRole("article").filter({ hasText: text });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const card = (page: Page) => messageList(page).getByTestId("run-card");

/** A chat with an agent as a member. */
function seed(api: FakeApi) {
  const agent = api.addAgent("Scout");
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "agent", agent);
  return { chat, agent };
}

async function post(page: Page, text: string) {
  await composer(page).fill(text);
  await composer(page).press("Enter");
}

test("mentioning an agent shows its card at once, ticking and counting, then posts its reply", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  // Long enough to watch the clock tick: a model step a poll, a poll a second.
  api.agentTurns.set(agent.id, { steps: 4, tokensPerStep: 600, reply: "Three **sources** found." });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await post(page, `@[Scout](agent:${agent.id}) find sources`);
  // The acknowledgement and its card come with the message, before any poll.
  const ack = message(page, "Reading the chat…");
  await expect(ack).toBeVisible();
  await expect(ack.getByText("Scout", { exact: true })).toBeVisible();
  await expect(card(page)).toBeVisible();

  // Running: the elapsed time ticks on its own, and the counters follow the run's.
  await expect(page.getByTestId("run-card-status")).toHaveText("Running");
  const elapsed = page.getByTestId("run-card-elapsed");
  await expect(elapsed).toHaveText(/^\d+s$/);
  const first = await elapsed.textContent();
  await expect(elapsed).not.toHaveText(first!);
  const counters = page.getByTestId("run-card-counters");
  await expect(counters).toHaveText("1 tool call · 600 tokens");
  await expect(counters).toHaveText("2 tool calls · 1.2k tokens");

  // When the turn ends, the reply is posted back from the agent, and the card stops there.
  const reply = message(page, "Three sources found.");
  await expect(reply).toBeVisible({ timeout: 10_000 });
  await expect(reply.getByText("Scout", { exact: true })).toBeVisible();
  await expect(reply.locator("strong")).toHaveText("sources");
  await expect(page.getByTestId("run-card-status")).toHaveText("Succeeded");
  await expect(counters).toHaveText("4 tool calls · 2.4k tokens");
  const stopped = await elapsed.textContent();
  await page.waitForTimeout(1500);
  await expect(elapsed).toHaveText(stopped!);

  // Clicking the card opens the run's page.
  const [run] = [...api.agentRuns.values()];
  expect(run.title).toBe("@Scout find sources");
  await card(page).click();
  await expect(page).toHaveURL(inOrg(`/agents/${agent.id}/runs/${run.id}`));
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");

  // Where the run is listed with the agent's others, marked as started from the chat.
  await page.goto(inOrg(`/agents/${agent.id}/runs`));
  await expect(page.getByTestId("started-from-chat")).toHaveText(/Started from the chat Design/);
});

test("a failed turn posts its error and fails the card", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  api.agentTurns.set(agent.id, { steps: 1, tokensPerStep: 100, error: "model overloaded" });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await post(page, `@[Scout](agent:${agent.id}) go`);
  await expect(message(page, "The run failed: model overloaded")).toBeVisible();
  await expect(page.getByTestId("run-card-status")).toHaveText("Failed");
});

test("an agent and a workflow mentioned together each get a card", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  const digest = api.addWorkflow("Digest", [{ key: "sum", agent: api.addAgent("Summer") }]);
  api.addChatTarget(chat, "workflow", digest);
  await page.goto(inOrg(`/chats/${chat.id}`));

  await post(page, `@[Scout](agent:${agent.id}) and @[Digest](workflow:${digest.id}), please`);
  await expect(card(page)).toHaveCount(2);
  await expect(message(page, "sum done")).toBeVisible();
  await expect(message(page, /^.*Done: @Scout and @Digest, please/)).toBeVisible();
});

test("an agent's card whose run has been deleted says so", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  await page.goto(inOrg(`/chats/${chat.id}`));
  await post(page, `@[Scout](agent:${agent.id}) go`);
  await expect(message(page, "Done: @Scout go")).toBeVisible({ timeout: 10_000 });

  const [run] = [...api.agentRuns.values()];
  api.agentRuns.delete(run.id);
  await page.reload();
  await expect(card(page)).toContainText("The run has been deleted.");
  await expect(card(page).getByText("Succeeded")).toBeVisible();
  await expect(messageList(page).getByRole("link", { name: /Open run/ })).toHaveCount(0);
});

test("an agent's card whose turn ended asking for a schema change shows the run waiting", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  const run = api.addAgentRun(agent, "@Scout keep notes", 2, { status: "succeeded" });
  const invocation = api.addChatRunCard(chat, "agent", agent, run.id);
  invocation.status = "running";
  const request = api.addSchemaChange({ run_id: run.id, summary: "Create table data.notes" });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await expect(page.getByTestId("run-card-status")).toHaveText("Waiting for you");

  request.status = "applied";
  request.resumed_at = "2026-10-14T10:00:01Z";
  await page.reload();
  await expect(page.getByTestId("run-card-status")).toHaveText("Succeeded");
});
