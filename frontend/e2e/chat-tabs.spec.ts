import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type ChatUser, inOrg } from "./fake-api";

/** The chat page splits into tabs: Chat (the room) and Members. */

const sam: ChatUser = { id: 2, first_name: "Sam", last_name: "Jones" };
// How often the chat list asks again, for unread counts (CHAT_LIST_POLL_MS).
const LIST_POLL_MS = 5000;

const tabs = (page: Page) => page.getByRole("tablist", { name: "Chat details" });
const tab = (page: Page, name: string | RegExp) => tabs(page).getByRole("tab", { name });
const panel = (page: Page) => page.getByRole("tabpanel");
const chatLink = (page: Page, name: string) =>
  page.getByRole("list", { name: "Chats" }).getByRole("link").filter({ hasText: name });
const messageList =(page: Page) => page.getByRole("log", { name: "Messages" });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });

test("opens on the Chat tab, with the header and actions above the tabs", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design", { description: "Where design happens" });
  api.addChatMember(chat, sam);
  api.addChatMessage(chat, "Welcome, all", sam);
  await page.goto(inOrg(`/chats/${chat.id}`));

  await expect(tabs(page)).toBeVisible();
  await expect(tab(page, "Chat")).toHaveAttribute("aria-selected", "true");
  await expect(tab(page, "Members 2")).toHaveAttribute("aria-selected", "false");
  await expect(panel(page).getByRole("log", { name: "Messages" })).toContainText("Welcome, all");
  // The members aren't shown on the Chat tab.
  await expect(page.getByRole("list", { name: "Users" })).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/chats/${chat.id}$`));

  // Title, description and actions are outside the panel, above the tabs.
  const heading = page.getByRole("heading", { name: "Design", level: 2 });
  await expect(heading).toBeVisible();
  await expect(heading.locator("..")).toContainText("Where design happens");
  await expect(panel(page).getByText("Where design happens")).toHaveCount(0);
  await expect(panel(page).getByRole("button", { name: "Delete" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete" })).toBeVisible();
});

test("switches to Members, keeps the tab in the URL, and a deep link opens it", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  api.addChatMember(chat, sam);
  await page.goto(inOrg(`/chats/${chat.id}`));

  await tab(page, /Members/).click();
  await expect(tab(page, /Members/)).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/\/members$/);
  await expect(panel(page).getByRole("list", { name: "Users" }).getByRole("listitem")).toHaveText([
    /Tester.*owner/,
    /Sam/,
  ]);
  // The room is unmounted while Members is open.
  await expect(messageList(page)).toHaveCount(0);

  // A reload, or a shared link, opens on the same tab.
  await page.reload();
  await expect(tab(page, /Members/)).toHaveAttribute("aria-selected", "true");
  await page.goto(inOrg(`/chats/${chat.id}/members`));
  await expect(panel(page).getByRole("list", { name: "Users" })).toBeVisible();

  // Back to the first tab drops the parameter.
  await tab(page, "Chat").click();
  await expect(page).toHaveURL(new RegExp(`/chats/${chat.id}$`));
  await expect(composer(page)).toBeVisible();
});

test("an unsent message survives switching to Members and back, but not to another chat", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const design = api.addChat("Design");
  const other = api.addChat("Other");
  await page.goto(inOrg(`/chats/${design.id}`));

  await composer(page).fill("Half a thought");
  await tab(page, /Members/).click();
  await expect(composer(page)).toHaveCount(0);
  await tab(page, "Chat").click();
  await expect(composer(page)).toHaveValue("Half a thought");

  // Another chat starts with an empty composer, and coming back still has the draft.
  await chatLink(page, "Other").click();
  await expect(page).toHaveURL(inOrg(`/chats/${other.id}`));
  await expect(composer(page)).toHaveValue("");
  await chatLink(page, "Design").click();
  await expect(composer(page)).toHaveValue("Half a thought");

  // Once sent, it's gone from the composer for good.
  await composer(page).press("Enter");
  await expect(messageList(page)).toContainText("Half a thought");
  await expect(composer(page)).toHaveValue("");
  await tab(page, /Members/).click();
  await tab(page, "Chat").click();
  await expect(composer(page)).toHaveValue("");
});

test("an unsent message survives the chat's Edit page, another section and a reload", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const design = api.addChat("Design");
  await page.goto(inOrg(`/chats/${design.id}`));
  await composer(page).fill("Half a thought");

  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page).toHaveURL(inOrg(`/chats/${design.id}/edit`));
  await page.goBack();
  await expect(composer(page)).toHaveValue("Half a thought");

  await page.goto(inOrg("/agents"));
  await page.goto(inOrg(`/chats/${design.id}`));
  await expect(composer(page)).toHaveValue("Half a thought");

  await page.reload();
  await expect(composer(page)).toHaveValue("Half a thought");

  await composer(page).press("Enter");
  await expect(messageList(page)).toContainText("Half a thought");
  await page.reload();
  await expect(composer(page)).toHaveValue("");
});

test("a message still sending when switching to Members and back isn't offered to send again", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  // The post waits until the test lets it through: a slow network.
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  await page.route(`**/api/chats/${chat.id}/messages`, async (route) => {
    if (route.request().method() === "POST") await released;
    await route.fallback();
  });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await composer(page).fill("Hello");
  await composer(page).press("Enter");
  await tab(page, /Members/).click();
  await tab(page, "Chat").click();
  // What's being sent has left the composer, so it can't be sent twice; typing goes on.
  await expect(composer(page)).toHaveValue("");
  await composer(page).fill("And another");

  release();
  await expect(messageList(page)).toContainText("Hello");
  // The first post finishing doesn't clear what was typed since.
  await expect(composer(page)).toHaveValue("And another");
  expect(api.writes.filter(([method]) => method === "POST")).toHaveLength(1);
});

test("a message that fails to send is back in the composer, with why", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  await page.route(`**/api/chats/${chat.id}/messages`, (route) =>
    route.request().method() === "POST"
      ? route.fulfill({ status: 503, json: { detail: "Try again later." } })
      : route.fallback(),
  );
  await page.goto(inOrg(`/chats/${chat.id}`));

  await composer(page).fill("Hello");
  await composer(page).press("Enter");
  await expect(page.getByText("Try again later.")).toBeVisible();
  await expect(composer(page)).toHaveValue("Hello");
  await expect(messageList(page)).not.toContainText("Hello");
});

test("messages aren't marked read while Members is open; back on Chat they are", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design", { owner: sam });
  api.addChatMessage(chat, "Welcome, all", sam);
  api.addChatMember(chat, api.me).last_read = chat.messages.at(-1)!.id;
  await page.goto(inOrg(`/chats/${chat.id}/members`));
  await expect(panel(page).getByRole("list", { name: "Users" })).toBeVisible();

  // Sam posts while Members is open: the list counts it, and nothing reads it.
  const posted = api.addChatMessage(chat, "Anyone around?", sam);
  await expect(chatLink(page, "Design").getByLabel("unread")).toHaveText("1", { timeout: LIST_POLL_MS + 2000 });
  expect(api.chatReads).not.toContainEqual([chat.id, posted.id]);

  // Back on Chat, the room reloads, scrolled to the newest, and reading it clears the count.
  await tab(page, "Chat").click();
  await expect(messageList(page)).toContainText("Anyone around?");
  await expect(chatLink(page, "Design").getByLabel("unread")).toHaveCount(0);
  expect(api.chatReads).toContainEqual([chat.id, posted.id]);
});
