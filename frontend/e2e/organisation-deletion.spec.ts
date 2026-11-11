import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Deleting an organisation: its creator types its name on the Organisation page's Settings tab,
 * and it's deleted after a grace period, which every page's banner dates; the creator can cancel
 * until then. Once it's gone, a member in no other organisation lands on the welcome page.
 */

const sam = { id: 2, first_name: "Sam", last_name: "Jones" };

test("the creator schedules deletion by typing the name, and a banner shows the date", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Delete organisation" });
  const button = section.getByRole("button", { name: "Delete organisation" });
  // Nothing to press until the name is typed, exactly.
  await expect(button).toBeDisabled();
  const confirm = section.getByRole("textbox", { name: "Type tester to confirm" });
  await confirm.fill("Tester");
  await expect(button).toBeDisabled();
  await confirm.fill("tester");
  await button.click();

  expect(api.writes).toEqual([["POST", orgApi("/organisation/deletion"), { name: "tester" }]]);
  const deletesAt = new Date(api.organisation.deletion!.deletes_at);
  const day = deletesAt.toLocaleDateString(undefined, { day: "numeric" });
  await expect(section).toContainText("Terry Tester scheduled it for deletion");
  // The shell's banner, on every page.
  const banner = page.getByRole("status", { name: "Deletion scheduled" });
  await expect(banner).toContainText("tester will be deleted on");
  await expect(banner).toContainText(day);
  await page.goto(inOrg("/agents"));
  await expect(page.getByRole("status", { name: "Deletion scheduled" })).toContainText("will be deleted on");
});

test("the creator cancels within the grace period, and the banner goes", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.deletion = {
    scheduled_at: "2026-10-20T09:00:00Z",
    scheduled_by: api.me,
    deletes_at: "2026-11-03T09:00:00Z",
  };
  await page.goto(inOrg("/organisation/settings"));
  await expect(page.getByRole("status", { name: "Deletion scheduled" })).toContainText(
    "tester will be deleted on Nov 3, 2026",
  );
  const section = page.getByRole("region", { name: "Delete organisation" });
  await section.getByRole("button", { name: "Cancel deletion" }).click();

  expect(api.writes).toEqual([["DELETE", orgApi("/organisation/deletion"), null]]);
  await expect(page.getByRole("status", { name: "Deletion scheduled" })).toBeHidden();
  await expect(section.getByRole("button", { name: "Delete organisation" })).toBeVisible();
});

test("another member sees the date but can't delete or cancel", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.creator = sam;
  api.organisation.members.unshift({ user: sam, joined_at: "2026-10-10T09:00:00Z", invite_id: null });
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Delete organisation" });
  await expect(section).toContainText("Only its creator can delete it.");
  await expect(section.getByRole("button")).toHaveCount(0);

  api.organisation.deletion = {
    scheduled_at: "2026-10-20T09:00:00Z",
    scheduled_by: sam,
    deletes_at: "2026-11-03T09:00:00Z",
  };
  await page.reload();
  await expect(page.getByRole("status", { name: "Deletion scheduled" })).toBeVisible();
  await expect(section).toContainText("Sam Jones scheduled it for deletion");
  await expect(section.getByRole("button")).toHaveCount(0);
});

test("an invitation link to an organisation scheduled for deletion doesn't open", async ({ page }) => {
  const api = await fakeApi(page);
  api.invites.push({
    id: "3f2a9c1e-0000-4000-8000-000000000000",
    creator: api.me,
    created_at: "2026-10-20T09:00:00Z",
    expires_at: "2026-12-20T09:00:00Z",
    token: "open-token",
  });
  api.organisation.deletion = {
    scheduled_at: "2026-10-20T09:00:00Z",
    scheduled_by: api.me,
    deletes_at: "2026-11-03T09:00:00Z",
  };
  api.signedInAs = null;
  await page.goto("/invite/open-token");
  await expect(page.getByText(/expired or been revoked/)).toBeVisible();
});

test("once it's deleted, a member with no organisation left lands on the welcome page", async ({ page }) => {
  const api = await fakeApi(page);
  // Deleted: it's no longer theirs, and they're in nothing else.
  api.member = false;
  api.otherMemberships = [];
  await page.goto(inOrg("/chats"));
  await expect(page).toHaveURL("/welcome");
  await expect(page.getByRole("heading", { name: "Welcome to aios" })).toBeVisible();
});
