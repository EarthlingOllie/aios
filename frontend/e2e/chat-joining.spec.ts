import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type ChatUser, inOrg, orgApi } from "./fake-api";

const sam: ChatUser = { id: 2, first_name: "Sam", last_name: "Jones" };
// How often the chat list asks again, for unread counts (CHAT_LIST_POLL_MS).
const LIST_POLL_MS = 5000;

const chatList = (page: Page) => page.getByRole("list", { name: "Chats" });
const listItem = (page: Page, name: string) => chatList(page).getByRole("link").filter({ hasText: name });
const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });

test("a public chat I'm not in is listed, read, and joined in one click", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design", { visibility: "public", owner: sam });
  api.addChatMessage(chat, "Welcome, all", sam);
  // A private one of theirs isn't there to find.
  api.addChat("Secret", { visibility: "private", owner: sam });
  await page.goto(inOrg("/chats"));

  await expect(chatList(page).getByRole("link")).toHaveCount(1);
  await expect(listItem(page, "Design")).toContainText("Not joined");
  await listItem(page, "Design").click();

  // Readable before joining, but not postable.
  await expect(messageList(page)).toContainText("Welcome, all");
  await expect(composer(page)).toHaveCount(0);
  await expect(page.getByText("Join the chat to post in it.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Leave" })).toHaveCount(0);

  await page.getByRole("button", { name: "Join", exact: true }).click();
  await expect(composer(page)).toBeVisible();
  await expect(listItem(page, "Design")).not.toContainText("Not joined");
  await page.getByRole("tab", { name: /Members/ }).click();
  await expect(page.getByRole("tabpanel", { name: /Members/ }).getByRole("listitem")).toHaveText([
    /Sam Jones.*owner/,
    /Terry Tester/,
  ]);
  expect(api.writes).toEqual([["POST", orgApi(`/chats/${chat.id}/join`), null]]);
  await page.getByRole("tab", { name: "Chat" }).click();

  // And having joined, they can leave again; it's back to one to join.
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Leave" }).click();
  await expect(page.getByRole("button", { name: "Join", exact: true })).toBeVisible();
  await expect(composer(page)).toHaveCount(0);
  await expect(listItem(page, "Design")).toContainText("Not joined");
  expect(api.writes.at(-1)).toEqual(["POST", orgApi(`/chats/${chat.id}/leave`), null]);
});

test("the owner is offered no Leave", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Mine", { visibility: "public" });
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(page.getByRole("heading", { name: "Mine", level: 2 })).toBeVisible();
  await expect(composer(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Leave" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Join", exact: true })).toHaveCount(0);
});

test("a joined chat counts what's new in the list, and reading it clears the count", async ({ page }) => {
  const api = await fakeApi(page);
  const design = api.addChat("Design", { visibility: "public", owner: sam });
  const other = api.addChat("Other");
  await page.goto(inOrg(`/chats/${design.id}`));
  await page.getByRole("button", { name: "Join", exact: true }).click();
  await expect(composer(page)).toBeVisible();
  // Another chat open, Sam posts in this one: the list's next read counts it.
  await listItem(page, "Other").click();
  await expect(page).toHaveURL(inOrg(`/chats/${other.id}`));
  api.addChatMessage(design, "Anyone around?", sam);
  api.addChatMessage(design, "Hello?", sam);
  await expect(listItem(page, "Design").getByLabel("unread")).toHaveText("2", { timeout: LIST_POLL_MS + 2000 });
  await expect(listItem(page, "Other").getByLabel("unread")).toHaveCount(0);

  // Opening it reads it to the bottom: the count clears.
  await listItem(page, "Design").click();
  await expect(messageList(page)).toContainText("Hello?");
  await expect(listItem(page, "Design").getByLabel("unread")).toHaveCount(0);
  expect(api.chatReads).toContainEqual([design.id, design.messages.at(-1)!.id]);
});
