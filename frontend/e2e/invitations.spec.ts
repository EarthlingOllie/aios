import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/** Invitation links on the Organisation page, the invite page, and leaving the organisation. */

const sam = { id: 2, first_name: "Sam", last_name: "Jones" };

test("the creator makes a link, copies it once, and revokes it", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/invitations"));
  const links = page.getByRole("region", { name: "Invitation links" });
  await expect(links.getByText("No open links.")).toBeVisible();

  await links.getByRole("combobox", { name: "Lasts" }).selectOption("30");
  await links.getByRole("button", { name: "Make a link" }).click();
  expect(api.writes).toEqual([["POST", orgApi("/organisation/invites"), { expires_in_days: 30 }]]);
  const made = links.getByRole("textbox", { name: "New link" });
  await expect(made).toHaveValue("http://localhost/invite/token-100");
  await expect(links.getByText("Copy it now: it won't be shown again.")).toBeVisible();
  await links.getByRole("button", { name: "Copy" }).click();
  await expect(links.getByRole("button", { name: "Copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "http://localhost/invite/token-100",
  );

  // Listed by name, with who made it and when it expires, but never shown again.
  await links.getByRole("button", { name: "Done" }).click();
  await expect(made).toBeHidden();
  const open = links.getByRole("list", { name: "Open links" }).getByRole("listitem");
  await expect(open).toHaveCount(1);
  await expect(open.first()).toContainText("link 00000064");
  await expect(open.first()).toContainText("Made by you");
  await expect(open.first()).toContainText("Expires");
  await expect(links).not.toContainText("token-100");
  await page.reload();
  await expect(open).toHaveCount(1);
  await expect(page.getByText("token-100")).toBeHidden();

  page.once("dialog", (dialog) => dialog.accept());
  await open.first().getByRole("button", { name: "Revoke" }).click();
  await expect(links.getByText("No open links.")).toBeVisible();
  expect(api.writes.at(-1)).toEqual([
    "DELETE",
    orgApi("/organisation/invites/00000064-0000-4000-8000-000000000000"),
    null,
  ]);
});

test("members show the link they came in through", async ({ page }) => {
  const api = await fakeApi(page);
  api.invites.push({
    id: "3f2a9c1e-0000-4000-8000-000000000000",
    creator: api.me,
    created_at: "2026-10-14T08:00:00Z",
    expires_at: "2026-10-21T08:00:00Z",
    token: "t",
  });
  api.organisation.members.push({
    user: sam,
    joined_at: "2026-10-15T09:00:00Z",
    invite_id: "3f2a9c1e-0000-4000-8000-000000000000",
  });
  await page.goto(inOrg("/organisation"));
  const members = page.getByRole("list", { name: "Members" }).getByRole("listitem");
  await expect(members.nth(1)).toContainText("through link 3f2a9c1e");
  await expect(members.nth(0)).not.toContainText("through link");
  await page.getByRole("tab", { name: "Invitations" }).click();
  await expect(
    page.getByRole("list", { name: "Open links" }).getByRole("listitem").first(),
  ).toContainText("link 3f2a9c1e");
});

test("a member who isn't the creator sees no invitation controls", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation = {
    ...api.organisation,
    name: "Acme",
    personal: false,
    creator: sam,
    members: [{ user: sam, joined_at: "2026-10-10T09:00:00Z", invite_id: null }, ...api.organisation.members],
  };
  await page.goto(inOrg("/organisation"));
  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Invitation links" })).toBeHidden();
  await expect(page.getByRole("button", { name: "Make a link" })).toBeHidden();
  // There's no Invitations tab, and its URL opens the Members tab.
  await expect(page.getByRole("tab", { name: "Invitations" })).toHaveCount(0);
  await page.goto(inOrg("/organisation/invitations"));
  await expect(page).toHaveURL(inOrg("/organisation"));
  await expect(page.getByRole("tab", { name: /Members/ })).toHaveAttribute("aria-selected", "true");
});

test("the last member can't leave, and is told to delete the organisation instead", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const leave = page.getByRole("region", { name: "Leave" });
  await expect(leave).toContainText(
    "You're its only member, so nobody would be left to see what's in it. Delete the organisation instead.",
  );
  await expect(leave.getByRole("button", { name: "Leave organisation" })).toBeDisabled();
});

test("the creator can't leave while others remain", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: sam, joined_at: "2026-10-15T09:00:00Z", invite_id: null });
  await page.goto(inOrg("/organisation/settings"));
  const leave = page.getByRole("region", { name: "Leave" });
  await expect(leave).toContainText("only its creator can invite people");
  await expect(leave.getByRole("button", { name: "Leave organisation" })).toBeDisabled();
});

test("a member leaves, and lands in the organisation they used most recently", async ({ page }) => {
  const api = await fakeApi(page);
  api.otherMemberships = [{ id: 9, name: "Globex", slug: "globex", is_creator: true }];
  api.organisation = {
    ...api.organisation,
    name: "Acme",
    personal: false,
    creator: sam,
    members: [{ user: sam, joined_at: "2026-10-10T09:00:00Z", invite_id: null }, ...api.organisation.members],
  };
  await page.goto(inOrg("/organisation/settings"));
  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Leave" })).toContainText(
    "land in the organisation you used most recently",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Leave organisation" }).click();
  expect(api.writes).toEqual([["POST", orgApi("/organisation/leave"), null]]);
  await expect(page).toHaveURL("/o/globex/");
});

test("signed in, the invite page names the organisation, and accepting joins it alongside the user's own", async ({
  page,
}) => {
  const api = await fakeApi(page);
  // `me`'s organisation has things and other members in it: they stay in it all the same.
  api.addAgent("Helper");
  api.organisation.members.push({ user: sam, joined_at: "2026-10-15T08:00:00Z", invite_id: null });
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  await expect(page.getByText("You'll stay in your other organisations.")).toBeVisible();
  await page.getByRole("button", { name: "Accept" }).click();
  expect(api.writes).toEqual([["POST", "/api/invites/abc/accept", null]]);
  await expect(page).toHaveURL("/o/acme/organisation");
  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  await page.getByRole("button", { name: "Acme", exact: true }).first().click();
  await expect(page.getByRole("menu", { name: "Organisations" }).getByRole("menuitem")).toHaveText([
    "Acme",
    "tester",
    "Create an organisation",
  ]);
});

test("a person in no organisation accepts from the invite page, shown without the shell", async ({ page }) => {
  const api = await fakeApi(page);
  api.member = false;
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Agents" })).toHaveCount(0);
  await page.getByRole("button", { name: "Accept" }).click();
  await expect(page).toHaveURL("/o/acme/organisation");
});

test("accepting after joining elsewhere meanwhile opens the organisation", async ({ page }) => {
  const api = await fakeApi(page);
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  // Joined Acme in another tab: the accept answers alike, and nothing changes.
  api.otherMemberships = [{ id: 7, name: "Acme", slug: "acme", is_creator: false }];
  await page.getByRole("button", { name: "Accept" }).click();
  await expect(page).toHaveURL("/o/acme/organisation");
  expect(api.otherMemberships).toEqual([{ id: 7, name: "Acme", slug: "acme", is_creator: false }]);
});

test("accepting a link revoked since it was opened says so", async ({ page }) => {
  const api = await fakeApi(page);
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  api.invitations.delete("abc");
  await page.getByRole("button", { name: "Accept" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "This invitation link has expired or been revoked, or never existed.",
  );
});

test("a link into the user's own organisation says they're in it, linking to it", async ({ page }) => {
  const api = await fakeApi(page);
  api.invitations.set("own", { organisation: { id: 1, name: "tester" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/own");
  await expect(page.getByText("You're already a member of tester.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept" })).toBeHidden();
  await page.getByRole("link", { name: "Organisation", exact: true }).click();
  await expect(page).toHaveURL(inOrg("/organisation"));
});

test("signed out, the invite page names the organisation and registers through it", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;
  api.invitations.set("abc/def", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc%2Fdef");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  // Signing in comes back here, to accept.
  await expect(page.getByRole("link", { name: "Sign in" })).toHaveAttribute(
    "href",
    "/login?next=%2Finvite%2Fabc%252Fdef",
  );

  await page.getByRole("link", { name: "Create an account to accept" }).click();
  await expect(page).toHaveURL(/\/register\?/);
  await page.getByLabel("First name").fill("Nadia");
  await page.getByLabel("Last name").fill("New");
  await page.getByLabel("Email").fill("nadia@example.com");
  await page.getByLabel("Password", { exact: true }).fill("a long passphrase");
  await page.getByLabel("Confirm password").fill("a long passphrase");
  await page.getByRole("button", { name: "Create account" }).click();

  // The registration carries the link, joining its organisation; then back to the link's page.
  await expect(page.getByText("You're already a member of Acme.")).toBeVisible();
  await expect(page).toHaveURL(/\/invite\/abc%2Fdef$/);
  expect(api.writes).toEqual([
    [
      "POST",
      "/api/auth/register",
      {
        first_name: "Nadia",
        last_name: "New",
        email: "nadia@example.com",
        password: "a long passphrase",
        password_confirm: "a long passphrase",
        invite: "abc/def",
      },
    ],
  ]);
});

test("an expired or revoked link says so plainly", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/invite/gone");
  await expect(page.getByRole("heading", { name: "This invitation link no longer works" })).toBeVisible();
  await expect(page.getByText("Ask whoever sent it for a new one.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept" })).toBeHidden();

  api.signedInAs = null;
  await page.reload();
  await expect(page.getByRole("heading", { name: "This invitation link no longer works" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Sign in|Create an account/ })).toBeHidden();
});
