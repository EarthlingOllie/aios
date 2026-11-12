import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type ChatUser, inOrg, orgApi } from "./fake-api";

const sam: ChatUser = { id: 2, first_name: "Sam", last_name: "Jones" };

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
/** The message whose body is `text`, exactly. */
const message = (page: Page, text: string) =>
  messageList(page).getByRole("article").filter({ has: page.getByText(text, { exact: true }) });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });

/** How far the message list is scrolled, and how far it can be. */
const scroll = (page: Page) =>
  messageList(page).evaluate((el) => ({ top: el.scrollTop, max: el.scrollHeight - el.clientHeight }));

test("posts a markdown message and shows it with its author", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(messageList(page)).toContainText("No messages yet");

  await composer(page).fill("Hello, **world**");
  await composer(page).press("Enter");

  const posted = message(page, "Hello, world");
  await expect(posted).toBeVisible();
  await expect(posted.locator("strong")).toHaveText("world");
  await expect(posted).toContainText("Terry Tester");
  await expect(composer(page)).toHaveValue("");
  expect(api.writes).toEqual([["POST", orgApi(`/chats/${chat.id}/messages`), { body: "Hello, **world**", mentions: [] }]]);
});

test("a message posted by someone else arrives on the chat's stream", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  api.addChatMember(chat, sam);
  const first = api.addChatMessage(chat, "Morning");
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(message(page, "Morning")).toBeVisible();

  api.addChatMessage(chat, "Morning, *all*", sam);
  const theirs = message(page, "Morning, all");
  await expect(theirs).toBeVisible({ timeout: 2 * api.streamRetryMs + 1000 });
  await expect(theirs).toContainText("Sam Jones");
  await expect(theirs.locator("em")).toHaveText("all");
  // No picture: their initial.
  await expect(theirs.getByRole("img", { name: "Sam Jones" })).toHaveText("S");
  // The stream resumed after the newest message it had; nothing asked for what's new.
  expect(api.streamReads).toContain(`?last_event_id=${first.id}`);
  expect(api.messageReads.filter((q) => q.startsWith("?after="))).toEqual([]);
});

test("loads history when scrolled to the top, keeping the reader's place", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  for (let n = 1; n <= 60; n++) api.addChatMessage(chat, `Message ${n}`);
  await page.goto(inOrg(`/chats/${chat.id}`));

  // The latest page, scrolled to the newest.
  await expect(message(page, "Message 60")).toBeInViewport();
  await expect(message(page, "Message 11")).toBeVisible();
  await expect(message(page, "Message 10")).toHaveCount(0);

  await messageList(page).evaluate((el) => el.scrollTo({ top: 0 }));
  await expect(message(page, "Message 10")).toHaveCount(1);
  await expect(message(page, "Message 1")).toHaveCount(1);
  // Still looking at the message that was at the top, not thrown to the oldest.
  await expect(message(page, "Message 11")).toBeInViewport();
  await expect(message(page, "Message 1")).not.toBeInViewport();
  expect(api.messageReads.some((q) => q.startsWith("?before="))).toBe(true);
});

test("follows new messages at the bottom, but not while reading older ones", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  for (let n = 1; n <= 30; n++) api.addChatMessage(chat, `Message ${n}`);
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(message(page, "Message 30")).toBeInViewport();

  // At the bottom: a new message scrolls into view.
  api.addChatMessage(chat, "Newer", sam);
  await expect(message(page, "Newer")).toBeInViewport();
  await expect.poll(async () => (await scroll(page)).max - (await scroll(page)).top).toBeLessThan(2);

  // Reading older ones: the view stays where it is.
  await messageList(page).evaluate((el) => el.scrollTo({ top: 100 }));
  api.addChatMessage(chat, "Newest", sam);
  await expect(message(page, "Newest")).toHaveCount(1);
  expect((await scroll(page)).top).toBe(100);
  await expect(message(page, "Newest")).not.toBeInViewport();
});

test("nothing polls the open chat: its stream brings what's new", async ({ page }) => {
  await page.clock.install();
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(messageList(page)).toContainText("No messages yet");
  await expect.poll(() => api.streamReads.length).toBeGreaterThan(0);

  const asked = api.messageReads.length;
  // Each batch of the fake's stream ends, and it's opened again: still no reads.
  for (let i = 0; i < 5; i++) {
    await page.clock.runFor(api.streamRetryMs);
    await page.waitForTimeout(100);
  }
  expect(api.messageReads.length).toBe(asked);
  api.addChatMessage(chat, "While you were away", sam);
  await page.clock.runFor(api.streamRetryMs);
  await expect(message(page, "While you were away")).toBeVisible();
  expect(api.messageReads.length).toBe(asked);
});

test("someone not in a chat reads it, and is told posting joins it", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Open", { owner: sam });
  api.addChatMessage(chat, "Welcome", sam);
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(message(page, "Welcome")).toBeVisible();
  await expect(composer(page)).toBeVisible();
  await expect(page.getByText("Everyone in the organisation can read this chat. Posting in it joins it.")).toBeVisible();
});
