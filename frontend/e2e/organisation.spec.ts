import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** The Organisation page: the organisation's name, creator and members, and renaming it. */

test("the shell names the organisation, and opens its page with the members", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({
    user: { id: 2, first_name: "Sam", last_name: "Jones" },
    joined_at: "2026-10-15T09:00:00Z",
    invite_id: null,
  });
  await page.goto(inOrg("/"));
  await page.getByRole("link", { name: "tester" }).first().click();
  await expect(page).toHaveURL(/\/organisation$/);

  await expect(page.getByRole("heading", { name: "tester" })).toBeVisible();
  await expect(page.getByText("Your personal organisation, created by you.")).toBeVisible();
  const members = page.getByRole("list", { name: "Members" }).getByRole("listitem");
  await expect(members).toHaveCount(2);
  await expect(members.nth(0)).toContainText("Tester");
  await expect(members.nth(0)).toContainText("Creator");
  await expect(members.nth(1)).toContainText("Sam Jones");
  // No handle: a member is their name.
  await expect(members.nth(1)).toContainText("Joined");
  await expect(members.nth(1)).not.toContainText("Creator");
});

test("the creator renames it, and the shell follows", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation"));
  await page.getByRole("button", { name: "Rename" }).click();
  const name = page.getByRole("textbox", { name: "Name" });
  await expect(name).toHaveValue("tester");
  await name.fill("Acme");
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  expect(api.writes).toEqual([["PUT", "/api/organisation", { name: "Acme" }]]);
  await expect(page.getByRole("link", { name: "Acme" }).first()).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Name" })).toBeHidden();
});

test("a name another organisation has is refused, and the name stays", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisationNamesTaken.add("Acme");
  await page.goto(inOrg("/organisation"));
  await page.getByRole("button", { name: "Rename" }).click();
  await page.getByRole("textbox", { name: "Name" }).fill("Acme");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert")).toHaveText("An organisation with that name already exists.");
  await expect(page.getByRole("heading", { name: "tester" })).toBeVisible();
  // Cancelling closes the form.
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("textbox", { name: "Name" })).toBeHidden();
});

test("another member sees who created it, and can't rename it", async ({ page }) => {
  const api = await fakeApi(page);
  const sam = { id: 2, first_name: "Sam", last_name: "Jones" };
  api.organisation.name = "Acme";
  api.organisation.personal = false;
  api.organisation.creator = sam;
  api.organisation.members.unshift({ user: sam, joined_at: "2026-10-10T09:00:00Z", invite_id: null });
  await page.goto(inOrg("/organisation"));
  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  await expect(page.getByText("Created by Sam Jones.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Rename" })).toBeHidden();
  const members = page.getByRole("list", { name: "Members" }).getByRole("listitem");
  await expect(members.nth(0)).toContainText("Creator");
  await expect(members.nth(1)).toContainText("Tester");
});

test("the Settings page links to it", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/settings"));
  await page.getByRole("main").getByRole("link", { name: "Organisation" }).click();
  await expect(page).toHaveURL(/\/organisation$/);
  await expect(page.getByRole("heading", { name: "tester" })).toBeVisible();
});
