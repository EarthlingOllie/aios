import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * The Organisation page splits into tabs, each a route: Members (`/organisation`), Invitations
 * (`/organisation/invitations`, the creator's) and Settings (`/organisation/settings`).
 */

const sam = { id: 2, first_name: "Sam", last_name: "Jones" };
const tab = (page: Page, name: string | RegExp) =>
  page.getByRole("tablist", { name: "Organisation details" }).getByRole("tab", { name });
const panel = (page: Page) => page.getByRole("tabpanel");

test("opens on Members, with the name and Rename above the tabs and the count on the tab", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: sam, joined_at: "2026-10-15T09:00:00Z", invite_id: null });
  await page.goto(inOrg("/organisation"));

  await expect(tab(page, "Members 2")).toHaveAttribute("aria-selected", "true");
  await expect(panel(page).getByRole("list", { name: "Members" }).getByRole("listitem")).toHaveCount(2);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Rename" })).toBeVisible();
  // The other tabs' content isn't on the page until opened.
  await expect(page.getByRole("region", { name: "Invitation links" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Leave" })).toHaveCount(0);
});

test("switching tabs moves to each tab's route, and the route opens its tab", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/organisation"));

  await tab(page, "Invitations").click();
  await expect(page).toHaveURL(inOrg("/organisation/invitations"));
  await expect(panel(page).getByRole("region", { name: "Invitation links" })).toBeVisible();

  await tab(page, "Settings").click();
  await expect(page).toHaveURL(inOrg("/organisation/settings"));
  await expect(panel(page).getByRole("region", { name: "Leave" })).toBeVisible();
  await page.reload();
  await expect(tab(page, "Settings")).toHaveAttribute("aria-selected", "true");

  await tab(page, /Members/).click();
  await expect(page).toHaveURL(inOrg("/organisation"));

  // The arrow keys move between tabs, as on the other tabbed pages.
  await tab(page, /Members/).focus();
  await page.keyboard.press("End");
  await expect(page).toHaveURL(inOrg("/organisation/settings"));
  await page.keyboard.press("ArrowLeft");
  await expect(page).toHaveURL(inOrg("/organisation/invitations"));
});

test("an unknown tab, and the first tab's own segment, land on /organisation", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/organisation/nope"));
  await expect(page).toHaveURL(inOrg("/organisation"));
  await page.goto(inOrg("/organisation/members"));
  await expect(page).toHaveURL(inOrg("/organisation"));
  await expect(tab(page, /Members/)).toHaveAttribute("aria-selected", "true");
});

test("renaming from another tab stays on it", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));

  await page.getByRole("button", { name: "Rename" }).click();
  await page.getByLabel("Name").fill("Acme");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  await expect(page).toHaveURL(inOrg("/organisation/settings"));
  await expect(tab(page, "Settings")).toHaveAttribute("aria-selected", "true");
  expect(api.writes).toEqual([["PUT", "/api/organisation", { name: "Acme" }]]);
});
