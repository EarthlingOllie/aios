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

test("the last member can't leave, and is told why", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const leave = page.getByRole("region", { name: "Leave" });
  await expect(leave).toContainText(
    "You're its only member, so you can't leave: an organisation can't be left empty.",
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

test("a member leaves, and lands in a personal organisation of their own", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation = {
    ...api.organisation,
    name: "Acme",
    personal: false,
    creator: sam,
    members: [{ user: sam, joined_at: "2026-10-10T09:00:00Z", invite_id: null }, ...api.organisation.members],
  };
  await page.goto(inOrg("/organisation/settings"));
  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Leave organisation" }).click();
  expect(api.writes).toEqual([["POST", orgApi("/organisation/leave"), null]]);
  await expect(page.getByRole("heading", { name: "tester" })).toBeVisible();
  await expect(page.getByText("Your personal organisation, created by you.")).toBeVisible();
  await expect(page.getByRole("link", { name: "tester" }).first()).toBeVisible();
});

test("signed in, the invite page names the organisation, and accepting from an empty one joins it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  // `me`'s personal organisation holds nothing but the documents assistant: it goes.
  api.assistantId = api.addAgent("Documents assistant").id;
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  await page.getByRole("button", { name: "Accept" }).click();
  expect(api.writes).toEqual([["POST", "/api/invites/abc/accept", null]]);
  await expect(page).toHaveURL(/\/organisation$/);
  await expect(page.getByRole("heading", { name: "Acme" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Acme" }).first()).toBeVisible();
});

test("accepting while in an organisation with others in it says why it can't", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: sam, joined_at: "2026-10-15T08:00:00Z", invite_id: null });
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await page.getByRole("button", { name: "Accept" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "You're a member of tester. You can be in only one organisation at a time.",
  );
  // Leaving is the way in: the link goes to where it's done.
  await expect(page.getByRole("alert")).toContainText("To join Acme, leave tester first");
  await page.getByRole("alert").getByRole("link", { name: "organisation settings" }).click();
  await expect(page).toHaveURL(/\/organisation\/settings$/);
  await expect(page.getByRole("region", { name: "Leave" })).toBeVisible();
});

test("accepting after joining elsewhere meanwhile says they're in it, without saying to leave", async ({ page }) => {
  const api = await fakeApi(page);
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  // Joined Acme in another tab: the accept is refused as already a member, and `me` says so.
  api.organisation = { ...api.organisation, id: 7, name: "Acme" };
  await page.getByRole("button", { name: "Accept" }).click();
  await expect(page.getByText("You're already a member of Acme.")).toBeVisible();
  await expect(page.getByText(/leave .* first/)).toBeHidden();
  await expect(page.getByRole("button", { name: "Accept" })).toBeHidden();
});

test("accepting a link revoked since it was opened says so, without saying to leave", async ({ page }) => {
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

test("accepting while the organisation has things in it lists them, linking to where each is deleted", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addAgent("Helper");
  api.addAgent("Other helper");
  api.addDocument("notes.md");
  api.invitations.set("abc", { organisation: { id: 7, name: "Acme" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/abc");
  await page.getByRole("button", { name: "Accept" }).click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("You're a member of tester, which has things in it.");
  const contents = alert.getByRole("list", { name: "What's in it" });
  await expect(contents.getByRole("listitem")).toHaveText(["2 agents", "1 document"]);
  await contents.getByRole("link", { name: "2 agents" }).click();
  await expect(page).toHaveURL(/\/agents$/);
});

test("a link into the user's own organisation says they're in it", async ({ page }) => {
  const api = await fakeApi(page);
  api.invitations.set("own", { organisation: { id: 1, name: "tester" }, expires_at: "2026-10-21T08:00:00Z" });
  await page.goto("/invite/own");
  await expect(page.getByText("You're already a member of tester.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept" })).toBeHidden();
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
