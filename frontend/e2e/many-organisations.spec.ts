import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Many organisations: signing up makes nothing and lands on the welcome page, which creates one;
 * the shell's switcher lists every organisation the person is in, with "Create an organisation";
 * leaving lands in the organisation used most recently, or on the welcome page.
 */

const sam = { id: 2, first_name: "Sam", last_name: "Jones" };
const globex = { id: 2, name: "Globex", slug: "globex", is_creator: false, deletes_at: null };
const initech = { id: 3, name: "Initech", slug: "initech", is_creator: true, deletes_at: null };

async function register(page: Page) {
  await page.getByLabel("First name").fill("Nadia");
  await page.getByLabel("Last name").fill("New");
  await page.getByLabel("Email").fill("nadia@example.com");
  await page.getByLabel("Password", { exact: true }).fill("a long passphrase");
  await page.getByLabel("Confirm password").fill("a long passphrase");
  await page.getByRole("button", { name: "Create account" }).click();
}

/** The shell's organisation switcher, opened. */
async function openSwitcher(page: Page, current: string) {
  await page.getByRole("button", { name: current, exact: true }).first().click();
  return page.getByRole("menu", { name: "Organisations" });
}

test("registering without an invitation lands on the welcome page, which creates an organisation and opens it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.signedInAs = null;
  await page.goto("/register");
  await register(page);

  await expect(page).toHaveURL("/welcome");
  await expect(page.getByRole("heading", { name: "Welcome to aios" })).toBeVisible();
  // It explains invitation links, the other way in.
  await expect(page.getByText(/invitation link/)).toBeVisible();
  // Nothing was made by signing up.
  expect(api.writes.map(([method, path]) => [method, path])).toEqual([["POST", "/api/auth/register"]]);

  await page.getByLabel("Organisation name").fill("  Nadia's Team ");
  await page.getByRole("button", { name: "Create organisation" }).click();
  expect(api.writes.at(-1)).toEqual(["POST", "/api/organisations", { name: "Nadia's Team" }]);
  await expect(page).toHaveURL(/\/o\/nadia-s-team\//);
  await expect(page.getByRole("button", { name: "Nadia's Team", exact: true }).first()).toBeVisible();
});

test("a person in no organisation is sent to the welcome page from anywhere", async ({ page }) => {
  const api = await fakeApi(page);
  api.member = false;
  // A link to an organisation too: one they were in has gone (deleted, say), and nothing else is
  // theirs.
  for (const path of ["/", "/agents", inOrg("/agents")]) {
    await page.goto(path);
    await expect(page).toHaveURL("/welcome");
    await expect(page.getByRole("heading", { name: "Welcome to aios" })).toBeVisible();
  }
});

test("the welcome page creates an organisation with a name another has", async ({ page }) => {
  const api = await fakeApi(page);
  api.member = false;
  api.otherMemberships = [];
  await page.goto("/welcome");
  await page.getByLabel("Organisation name").fill("tester");
  await page.getByRole("button", { name: "Create organisation" }).click();
  await expect(page).toHaveURL(/\/o\/tester\//);
  expect(api.writes.at(-1)).toEqual(["POST", "/api/organisations", { name: "tester" }]);
});

test("the switcher lists every organisation the person is in, and offers to create one", async ({ page }) => {
  const api = await fakeApi(page);
  api.otherMemberships = [globex, initech];
  await page.goto(inOrg("/agents"));
  const menu = await openSwitcher(page, "tester");
  await expect(menu.getByRole("menuitem")).toHaveText(["Globex", "Initech", "tester", "Create an organisation"]);
  await expect(menu.getByRole("menuitem", { name: "tester" })).toHaveAttribute("aria-current", "true");

  // Another organisation opens in its own link: the same tab, a new page load.
  await expect(menu.getByRole("menuitem", { name: "Globex" })).toHaveAttribute("href", "/o/globex/");
  // The current one opens its Organisation page.
  await menu.getByRole("menuitem", { name: "tester" }).click();
  await expect(page).toHaveURL(inOrg("/organisation"));
  await expect(menu).toBeHidden();

  await (await openSwitcher(page, "tester")).getByRole("menuitem", { name: "Create an organisation" }).click();
  await expect(page).toHaveURL("/welcome");
  await expect(page.getByRole("heading", { name: "Create an organisation" })).toBeVisible();
  await page.getByLabel("Organisation name").fill("Side Project");
  await page.getByRole("button", { name: "Create organisation" }).click();
  await expect(page).toHaveURL(/\/o\/side-project\//);
  const after = await openSwitcher(page, "Side Project");
  await expect(after.getByRole("menuitem")).toHaveText([
    "Side Project",
    "Globex",
    "Initech",
    "tester",
    "Create an organisation",
  ]);
});

test("the switcher closes on Escape", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/agents"));
  const menu = await openSwitcher(page, "tester");
  await expect(menu).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
});

test("leaving lands in the organisation used most recently", async ({ page }) => {
  const api = await fakeApi(page);
  api.joinedEarlier = true;
  api.otherMemberships = [globex, initech];
  api.organisation = {
    ...api.organisation,
    creator: sam,
    members: [{ user: sam, joined_at: "2026-10-10T09:00:00Z", invite_id: null }, ...api.organisation.members],
  };
  // Used Initech, then tester.
  await page.goto("/o/initech/");
  await expect(page.getByRole("button", { name: "Initech", exact: true }).first()).toBeVisible();
  await page.goto(inOrg("/organisation/settings"));
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Leave organisation" }).click();
  expect(api.writes).toEqual([["POST", orgApi("/organisation/leave"), null]]);
  await expect(page).toHaveURL("/o/initech/");
});

test("leaving the only organisation lands on the welcome page", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation = {
    ...api.organisation,
    creator: sam,
    members: [{ user: sam, joined_at: "2026-10-10T09:00:00Z", invite_id: null }, ...api.organisation.members],
  };
  await page.goto(inOrg("/organisation/settings"));
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Leave organisation" }).click();
  await expect(page).toHaveURL("/welcome");
  await expect(page.getByRole("heading", { name: "Welcome to aios" })).toBeVisible();
});
