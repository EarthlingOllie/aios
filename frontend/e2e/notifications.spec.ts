import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/**
 * Notifications: the navigation polls for them and shows an unread badge, with a list whose
 * items open what they're about. A schema change request waiting for the user is the first kind.
 */

// How often the navigation asks again (NOTIFICATIONS_POLL_MS); the page's clock is run on by it.
const POLL_MS = 10_000;

const bell = (page: Page) => page.getByRole("button", { name: /^Notifications/ });
const list = (page: Page) => page.getByRole("region", { name: "Notifications" });
const card = (page: Page, summary = "Create table data.contacts") =>
  page.getByRole("region", { name: `Schema change: ${summary}` });

test("a new request shows an unread badge without a reload", async ({ page }) => {
  await page.clock.install();
  const api = await fakeApi(page);
  await page.goto("/agents");
  await expect(bell(page)).toHaveAccessibleName("Notifications");

  api.addSchemaChange();
  await page.clock.runFor(POLL_MS);

  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");
  await expect(bell(page)).toContainText("1");
});

test("an item opens the request on the Data page, and deciding it clears the badge", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addSchemaChange({ summary: "Create table data.places" });
  const request = api.addSchemaChange();
  await page.goto("/agents");

  await bell(page).click();
  const item = list(page).getByRole("link", { name: /Create table data\.contacts/ });
  await expect(item).toContainText("A schema change is waiting for you");
  await item.click();

  await expect(page).toHaveURL(`/data?tab=changes&request=${request.id}`);
  await expect(list(page)).toBeHidden();
  await expect(card(page)).toHaveAttribute("aria-current", "true");
  await expect(card(page, "Create table data.places")).not.toHaveAttribute("aria-current", "true");
  await expect(bell(page)).toHaveAccessibleName("Notifications, 2 unread");

  await card(page).getByRole("button", { name: "Approve" }).click();

  // Straight away, not at the next poll.
  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");
});

test("rejecting clears the badge too", async ({ page }) => {
  const api = await fakeApi(page);
  const request = api.addSchemaChange();
  await page.goto(`/data?tab=changes&request=${request.id}`);
  await expect(bell(page)).toHaveAccessibleName("Notifications, 1 unread");

  await card(page).getByRole("button", { name: "Reject…" }).click();
  await card(page).getByRole("button", { name: "Reject", exact: true }).click();

  await expect(bell(page)).toHaveAccessibleName("Notifications");
});

test("marking one read clears it from the badge and keeps it in the list", async ({ page }) => {
  const api = await fakeApi(page);
  api.addSchemaChange();
  const [notification] = api.notifications;
  await page.goto("/agents");

  await bell(page).click();
  await list(page).getByRole("button", { name: "Mark read" }).click();

  await expect(bell(page)).toHaveAccessibleName("Notifications");
  await expect(list(page).getByRole("link", { name: /Create table data\.contacts/ })).toBeVisible();
  await expect(list(page).getByRole("button", { name: "Mark read" })).toHaveCount(0);
  expect(api.writes).toContainEqual(["POST", `/api/notifications/${notification.id}/read`, null]);
});

test("another user's notifications never show", async ({ page }) => {
  const api = await fakeApi(page);
  api.addNotification({ mine: false, ref: { request_id: "theirs" } });
  await page.goto("/agents");

  await expect(bell(page)).toHaveAccessibleName("Notifications");
  await bell(page).click();
  await expect(list(page)).toContainText("Nothing yet");
});
