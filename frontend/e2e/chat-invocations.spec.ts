import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";
import { setHidden } from "./visibility";

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const message = (page: Page, text: string | RegExp) =>
  messageList(page).getByRole("article").filter({ hasText: text });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const card = (page: Page) => messageList(page).getByTestId("run-card");
const node = (page: Page, key: string) => card(page).locator(`.react-flow__node[data-id="${key}"]`);

/** A chat with a three-node workflow as a member. */
function seed(api: FakeApi) {
  const report = api.addWorkflow(
    "Report",
    [
      { key: "research", agent: api.addAgent("Researcher") },
      { key: "write", agent: api.addAgent("Writer") },
      { key: "review", agent: api.addAgent("Critic") },
    ],
    [
      ["research", "write"],
      ["write", "review"],
    ],
  );
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "workflow", report);
  return { chat, report };
}

async function mention(page: Page, workflow: { id: number; name: string }, text: string) {
  await composer(page).fill(`@[${workflow.name}](workflow:${workflow.id}) ${text}`);
  await composer(page).press("Enter");
}

test("mentioning a workflow shows its card at once, follows the run and posts the result", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, report } = seed(api);
  await page.goto(`/chats/${chat.id}`);
  await expect(messageList(page)).toContainText("No messages yet");

  await mention(page, report, "Summarise the week");
  // The acknowledgement and its card come with the message, before any poll.
  const ack = message(page, "Reading the chat…");
  await expect(ack).toBeVisible();
  await expect(ack.getByText("Report", { exact: true })).toBeVisible();
  await expect(card(page)).toBeVisible();
  expect(api.writes).toEqual([
    [
      "POST",
      `/api/chats/${chat.id}/messages`,
      {
        body: `@[Report](workflow:${report.id}) Summarise the week`,
        mentions: [{ kind: "workflow", id: report.id }],
      },
    ],
  ]);

  // Each poll moves the run on; the card draws where it is.
  await expect(node(page, "research")).toHaveAttribute("data-state", "succeeded");
  await expect(node(page, "write")).toHaveAttribute("data-state", "running");
  await expect(card(page)).toContainText("1 of 3 nodes done");
  await expect(page.getByTestId("run-card-status")).toHaveText("Running");

  // When it ends, the output is posted back from the workflow, and the card stops there.
  const result = message(page, "review done");
  await expect(result).toBeVisible();
  await expect(result.getByText("Report", { exact: true })).toBeVisible();
  await expect(page.getByTestId("run-card-status")).toHaveText("Succeeded");
  await expect(card(page)).toContainText("3 of 3 nodes done");
  await expect(node(page, "review")).toHaveAttribute("data-state", "succeeded");
  const [run] = [...api.runs.values()];
  expect(run.input).toBe("@Report Summarise the week");

  // Clicking the card opens the run's page.
  await card(page).click();
  await expect(page).toHaveURL(`/workflows/${report.id}/runs/${run.id}`);
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded");

  // Where the run is listed with the workflow's others, marked as started from the chat.
  await page.goto(`/workflows/${report.id}/runs`);
  await expect(page.getByTestId("started-from-chat")).toHaveText(/Started from the chat Design/);
});

test("a run that can't start fails its card with the reason", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, report } = seed(api);
  api.refuseStart.set(report.id, "This workflow has no nodes");
  await page.goto(`/chats/${chat.id}`);

  await mention(page, report, "Go");
  await expect(card(page)).toContainText("Couldn't start the run: This workflow has no nodes");
  await expect(card(page).getByText("Failed")).toBeVisible();
  // Nothing to open.
  await expect(messageList(page).getByRole("link", { name: /Open run/ })).toHaveCount(0);
  // And it's posted back: the card's message and the result say so.
  await expect(message(page, "Couldn't start the run: This workflow has no nodes")).toHaveCount(2);
});

test("several workflows mentioned in one message each get a card", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, report } = seed(api);
  const digest = api.addWorkflow("Digest", [{ key: "sum", agent: api.addAgent("Summer") }]);
  api.addChatTarget(chat, "workflow", digest);
  await page.goto(`/chats/${chat.id}`);

  await composer(page).fill(`@[Report](workflow:${report.id}) and @[Digest](workflow:${digest.id}), please`);
  await composer(page).press("Enter");
  await expect(card(page)).toHaveCount(2);
  await expect(message(page, "sum done")).toBeVisible();
  // A poll a second, a node a poll.
  await expect(message(page, "review done")).toBeVisible({ timeout: 10_000 });
});

test("a card whose run has been deleted says so", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, report } = seed(api);
  await page.goto(`/chats/${chat.id}`);
  await mention(page, report, "Go");
  // A poll a second, a node a poll.
  await expect(message(page, "review done")).toBeVisible({ timeout: 10_000 });

  const [run] = [...api.runs.values()];
  api.runs.delete(run.id);
  await page.reload();
  await expect(card(page)).toContainText("The run has been deleted.");
  // It still says how the run went.
  await expect(card(page).getByText("Succeeded")).toBeVisible();
  await expect(messageList(page).getByRole("link", { name: /Open run/ })).toHaveCount(0);
  await expect(message(page, "review done")).toBeVisible();
});

test("a live run is followed every second even while the tab is hidden", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, report } = seed(api);
  await page.goto(`/chats/${chat.id}`);
  await mention(page, report, "Go");
  await expect(card(page)).toBeVisible();
  await setHidden(page, true);
  // Hidden, a quiet chat is read every 30 seconds; this one has a run going.
  await expect(message(page, "review done")).toBeVisible({ timeout: 10_000 });
});
