import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type ChatUser, type FakeApi, inOrg } from "./fake-api";

/**
 * Live updates: an open chat follows its stream (drafts, reconnecting, falling back to reads
 * while it can't open, closing for good on `gone`), and the chat list follows the person's.
 */

const sam: ChatUser = { id: 2, first_name: "Sam", last_name: "Jones" };

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const message = (page: Page, text: string | RegExp) =>
  messageList(page).getByRole("article").filter({ hasText: text });
const draft = (page: Page) => messageList(page).getByRole("article", { name: "Reply being written" });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const notLive = (page: Page) => page.getByRole("status", { name: "Not live" });

function seed(api: FakeApi) {
  const agent = api.addAgent("Scout");
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "agent", agent);
  api.addChatMember(chat, sam);
  return { chat, agent };
}

async function post(page: Page, text: string) {
  await composer(page).fill(text);
  await composer(page).press("Enter");
}

test("a draft grows as the reply is written, and the reply replaces it in place", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  api.agentTurns.set(agent.id, {
    steps: 3,
    tokensPerStep: 100,
    drafts: ["Three", "Three sources", "Three sources found"],
    reply: "Three **sources** found.",
  });
  await page.goto(inOrg(`/chats/${chat.id}`));
  await post(page, `@[Scout](agent:${agent.id}) find sources`);

  await expect(draft(page)).toContainText("Three");
  await expect(draft(page)).toContainText("writing…");
  await expect(draft(page)).toContainText("Scout");
  await expect(draft(page)).toContainText("Three sources");
  // The reply takes its place, at the bottom, once; no draft is left.
  const reply = message(page, "Three sources found.");
  await expect(reply).toBeVisible({ timeout: 10_000 });
  await expect(draft(page)).toHaveCount(0);
  await expect(messageList(page).getByRole("article").last()).toContainText("Three sources found.");
  await expect(reply).toHaveCount(1);
});

test("opening a chat mid-reply shows the draft so far", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  api.agentTurns.set(agent.id, { steps: 8, tokensPerStep: 100, drafts: ["Nothing much, but"] });
  await page.goto(inOrg(`/chats/${chat.id}`));
  await post(page, `@[Scout](agent:${agent.id}) what's new?`);
  await expect(draft(page)).toContainText("Nothing much, but");

  // Opened afresh, mid-reply: the stream brings the draft as it opens.
  await page.reload();
  await expect(message(page, "what's new?")).toBeVisible();
  await expect(draft(page)).toContainText("Nothing much, but");
});

test("when a turn fails, its draft goes and the failure shows", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat, agent } = seed(api);
  api.agentTurns.set(agent.id, { steps: 2, tokensPerStep: 100, drafts: ["Half an"], error: "model overloaded" });
  await page.goto(inOrg(`/chats/${chat.id}`));
  await post(page, `@[Scout](agent:${agent.id}) go`);

  await expect(draft(page)).toContainText("Half an");
  await expect(message(page, "The run failed: model overloaded")).toBeVisible({ timeout: 10_000 });
  await expect(draft(page)).toHaveCount(0);
  await expect(page.getByTestId("run-card-status")).toHaveText("Failed");
});

test("a dropped stream catches up from the newest message shown, with nothing twice", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat } = seed(api);
  const seen = api.addChatMessage(chat, "Before", sam);
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(message(page, "Before")).toBeVisible();

  // Each batch of the fake's stream ends, as a dropped stream does; two arrive meanwhile.
  api.addChatMessage(chat, "Missed one", sam);
  api.addChatMessage(chat, "Missed two", sam);
  await expect(message(page, "Missed two")).toBeVisible();
  await expect(message(page, "Missed one")).toHaveCount(1);
  await expect(message(page, "Missed two")).toHaveCount(1);
  expect(api.streamReads[0]).toBe(`?visible=true&last_event_id=${seen.id}`);
  // Later openings resume after what was shown by then.
  await expect.poll(() => api.streamReads.at(-1)).not.toBe(`?visible=true&last_event_id=${seen.id}`);
  await expect(messageList(page).getByRole("article")).toHaveCount(3);
});

test("with live updates down, the chat reads every few seconds and recovers by itself", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat } = seed(api);
  api.streamsDown = true;
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(notLive(page)).toContainText("Live updates are paused");

  // Read, not streamed, within the fallback interval.
  api.addChatMessage(chat, "Still there?", sam);
  await expect(message(page, "Still there?")).toBeVisible({ timeout: 8000 });
  expect(api.messageReads.some((q) => q.startsWith("?after="))).toBe(true);

  // Back: the stream opens again after its retry hint, and the notice goes.
  api.streamsDown = false;
  await expect(notLive(page)).toHaveCount(0, { timeout: 5000 });
  const reads = api.messageReads.length;
  api.addChatMessage(chat, "Live again", sam);
  await expect(message(page, "Live again")).toBeVisible({ timeout: 3000 });
  expect(api.messageReads.length).toBe(reads);
});

test("leaving the organisation closes the chat's stream for good, saying so", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat } = seed(api);
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect.poll(() => api.streamReads.length).toBeGreaterThan(0);

  api.streamGone = "left_organisation";
  await expect(notLive(page)).toContainText("You've left this organisation");
  const opened = api.streamReads.length;
  await page.waitForTimeout(3 * api.streamRetryMs);
  // It doesn't try again, nor read instead.
  expect(api.streamReads.length).toBe(opened);
});

test("archiving turns an open chat read-only without closing it", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat } = seed(api);
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(composer(page)).toBeVisible();
  await expect.poll(() => api.streamReads.length).toBeGreaterThan(0);

  // Another admin archives it.
  Object.assign(chat, { archived_at: "2026-11-10T09:00:00Z", archived_by: sam });
  await expect(page.getByRole("status", { name: "Read-only" })).toBeVisible();
  await expect(composer(page)).toHaveCount(0);
  // Still streaming: what's posted (by a run under way, say) still arrives.
  api.addChatMessage(chat, "Posted after archiving", sam);
  await expect(message(page, "Posted after archiving")).toBeVisible();
});

test("the chat list's unread counts and chats follow the person's stream", async ({ page }) => {
  const api = await fakeApi(page);
  const { chat } = seed(api);
  const other = api.addChat("Elsewhere");
  api.addChatMember(other, api.me);
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect.poll(() => api.personStreamReads).toBeGreaterThan(0);
  const lists = page.getByRole("link", { name: /Elsewhere/ });
  await expect(lists).toBeVisible();

  api.addChatMessage(other, "Ping", sam);
  api.addChatMessage(other, "Pong", sam);
  await expect(lists.getByLabel("unread")).toHaveText("2");
  // A chat made meanwhile appears.
  api.addChat("Brand new");
  await expect(page.getByRole("link", { name: /Brand new/ })).toBeVisible();
});
