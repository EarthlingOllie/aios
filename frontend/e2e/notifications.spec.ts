import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Notifications: the navigation's bell, with an unread badge and a list of the person's
 * notifications from every organisation they're in, each labelled with its organisation. They
 * reach it through the person's stream; nothing polls while it's open. An item opens what it's
 * about, in its organisation, and is read.
 */

const bell = (page: Page) => page.getByRole("button", { name: /^Notifications/ });
const list = (page: Page) => page.getByRole("region", { name: "Notifications" });
const card = (page: Page, summary = "Create table data.contacts") =>
  page.getByRole("region", { name: `Schema change: ${summary}` });

test("new and read notifications reach the bell through the person's stream", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/agents"));
  await expect(bell(page)).toHaveAccessibleName("Notifications");

  api.addSchemaChange();
  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");
  await expect(bell(page)).toContainText("1");
  expect(api.personStreamReads).toBeGreaterThan(0);

  // Read elsewhere (another tab, deciding it): the stream says so.
  api.notifications[0].read_at = "2026-10-14T09:00:00Z";
  await expect(bell(page)).toHaveAccessibleName("Notifications");
});

test("nothing polls while the person's stream is open", async ({ page }) => {
  await page.clock.install();
  const api = await fakeApi(page);
  // The stream stays as it is for a minute after it opens: nothing new comes on it meanwhile.
  api.streamRetryMs = 60_000;
  await page.goto(inOrg("/agents"));
  await expect(bell(page)).toHaveAccessibleName("Notifications");
  await expect.poll(() => api.personStreamReads).toBeGreaterThan(0);
  // Settled: the reads its openings asked for are done.
  await page.waitForTimeout(500);
  const asked = api.notificationLists;
  const opened = api.personStreamReads;

  for (let i = 0; i < 3; i++) {
    await page.clock.runFor(10_000);
    // Time for a request a timer started to reach the API.
    await page.waitForTimeout(300);
  }

  expect(api.notificationLists).toBe(asked);
  expect(api.personStreamReads).toBe(opened);
});

test("while the person's stream can't open, the bell reads every few seconds", async ({ page }) => {
  const api = await fakeApi(page);
  api.streamsDown = true;
  await page.goto(inOrg("/agents"));
  await expect(bell(page)).toHaveAccessibleName("Notifications");

  api.addSchemaChange();

  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread", { timeout: 10_000 });
});

test("an item opens the request on the Data page, and reads it", async ({ page }) => {
  const api = await fakeApi(page);
  api.addSchemaChange({ summary: "Create table data.places" });
  const request = api.addSchemaChange();
  await page.goto(inOrg("/agents"));
  await expect(bell(page)).toHaveAccessibleName("Notifications, 2 unread");

  await bell(page).click();
  const item = list(page).getByRole("link", { name: /Create table data\.contacts/ });
  await expect(item).toContainText("A schema change is waiting for you");
  await item.click();

  await expect(page).toHaveURL(inOrg(`/data/approvals?approval=${request.id}`));
  await expect(list(page)).toBeHidden();
  await expect(card(page)).toHaveAttribute("aria-current", "true");
  await expect(card(page, "Create table data.places")).not.toHaveAttribute("aria-current", "true");
  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");
});

test("deciding a request clears its badge straight away", async ({ page }) => {
  const api = await fakeApi(page);
  const request = api.addSchemaChange();
  await page.goto(inOrg(`/data/changes?request=${request.id}`));
  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");

  await card(page).getByRole("button", { name: "Approve" }).click();

  await expect(bell(page)).toHaveAccessibleName("Notifications");
});

test("rejecting clears the badge too", async ({ page }) => {
  const api = await fakeApi(page);
  const request = api.addSchemaChange();
  await page.goto(inOrg(`/data/changes?request=${request.id}`));
  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");

  await card(page).getByRole("button", { name: "Reject…" }).click();
  await card(page).getByRole("button", { name: "Reject", exact: true }).click();

  await expect(bell(page)).toHaveAccessibleName("Notifications");
});

test("marking one read clears it from the badge and keeps it in the list", async ({ page }) => {
  const api = await fakeApi(page);
  api.addSchemaChange();
  const [notification] = api.notifications;
  await page.goto(inOrg("/agents"));

  await bell(page).click();
  const item = list(page).getByRole("link", { name: /Create table data\.contacts/ });
  // Said, not only shown by a dot.
  await expect(item).toHaveAccessibleName(/^Unread/);
  await list(page).getByRole("button", { name: "Mark read" }).click();

  await expect(bell(page)).toHaveAccessibleName("Notifications");
  await expect(item).toBeVisible();
  await expect(item).not.toHaveAccessibleName(/Unread/);
  await expect(list(page).getByRole("button", { name: "Mark read" })).toHaveCount(0);
  expect(api.writes).toContainEqual(["POST", `/api/me/notifications/${notification.id}/read`, null]);
});

test("mark all read clears the badge and keeps them listed", async ({ page }) => {
  const api = await fakeApi(page);
  api.addSchemaChange({ summary: "Create table data.places" });
  api.addSchemaChange();
  await page.goto(inOrg("/agents"));
  await expect(bell(page)).toHaveAccessibleName("Notifications, 2 unread");

  await bell(page).click();
  await list(page).getByRole("button", { name: "Mark all read" }).click();

  await expect(bell(page)).toHaveAccessibleName("Notifications");
  await expect(list(page).getByRole("link")).toHaveCount(2);
  await expect(list(page).getByRole("button", { name: "Mark all read" })).toHaveCount(0);
  expect(api.writes).toContainEqual(["POST", "/api/me/notifications/read", null]);
});

test("each is labelled with its organisation", async ({ page }) => {
  const api = await fakeApi(page);
  api.otherMemberships = [{ id: 2, name: "Globex", slug: "globex", is_creator: false }];
  api.addSchemaChange();
  api.addNotification({
    organisation: { id: 2, slug: "globex", name: "Globex" },
    ref: { approval_id: "r9" },
    title: "A schema change is waiting for you",
    detail: "Create table data.orders",
  });
  await page.goto(inOrg("/agents"));

  await bell(page).click();
  await expect(list(page).getByRole("link", { name: /data\.contacts/ })).toContainText("In tester");
  await expect(list(page).getByRole("link", { name: /data\.orders/ })).toContainText("In Globex");
});

test("one from another organisation switches to it, opens there, and is read", async ({ page }) => {
  const api = await fakeApi(page);
  api.otherMemberships = [{ id: 2, name: "Globex", slug: "globex", is_creator: false }];
  const [notification] = [
    api.addNotification({
      organisation: { id: 2, slug: "globex", name: "Globex" },
      kind: "mention",
      ref: { chat_id: 40, message_id: 41 },
      title: "Ada Lovelace mentioned you in Launch",
      detail: "@Test look at this",
    }),
  ];
  await page.goto(inOrg("/agents"));

  await bell(page).click();
  const item = list(page).getByRole("link", { name: /Ada Lovelace mentioned you in Launch/ });
  await expect(item).toContainText("@Test look at this");
  await item.click();

  await expect(page).toHaveURL("/o/globex/chats/40");
  expect(api.writes).toContainEqual(["POST", `/api/me/notifications/${notification.id}/read`, null]);
});

test("reading a chat past a mention clears it", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Launch");
  const message = api.addChatMessage(chat, "Before");
  api.addNotification({
    kind: "mention",
    ref: { chat_id: chat.id, message_id: message.id },
    title: "Ada Lovelace mentioned you in Launch",
    detail: "@Test look",
  });
  await page.goto(inOrg("/agents"));
  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");

  await page.getByRole("link", { name: "Chats" }).first().click();
  await page.getByRole("link", { name: /Launch/ }).first().click();

  // Reading the chat to the bottom reads the notification about it.
  await expect(bell(page)).toHaveAccessibleName("Notifications");
});

test("one whose reference has gone isn't listed", async ({ page }) => {
  const api = await fakeApi(page);
  // An approval whose request no longer exists.
  api.addNotification({ ref: { approval_id: "gone" } });
  await page.goto(inOrg("/agents"));

  await expect(bell(page)).toHaveAccessibleName("Notifications");
  await bell(page).click();
  await expect(list(page)).toContainText("Nothing yet");
  expect(api.notifications[0].read_at).not.toBeNull();
});

test("another user's notifications never show", async ({ page }) => {
  const api = await fakeApi(page);
  api.addNotification({ mine: false, ref: { approval_id: "theirs" } });
  await page.goto(inOrg("/agents"));

  await expect(bell(page)).toHaveAccessibleName("Notifications");
  await bell(page).click();
  await expect(list(page)).toContainText("Nothing yet");
});
