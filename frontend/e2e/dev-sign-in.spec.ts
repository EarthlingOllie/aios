import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** The development "sign in as" picker, which the API offers only when DEV_LOGIN_ENABLED is on. */

const picker = (page: Page) => page.getByLabel("Development: sign in as...");

test("the sign-in page has no picker unless the API offers it", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;

  await page.goto("/login");

  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await expect(picker(page)).toHaveCount(0);
  await expect(page.getByText("dev only")).toHaveCount(0);
});

test("picking a user signs in as them, then goes where they were headed", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;
  api.devUsers = true;
  api.accounts.push(
    { id: 2, first_name: "Alice", last_name: "Smith", email: "alice@example.com", password: "x" },
    { id: 3, first_name: "Bob", last_name: "Abbott", email: "bob@example.com", password: "x" },
  );

  await page.goto("/settings?tab=x");
  await expect(page).toHaveURL("/login?next=%2Fsettings%3Ftab%3Dx");
  await expect(page.getByText("dev only")).toBeVisible();
  // By last name, then first name; each "First Last (email)".
  await expect(picker(page).locator("option:not([disabled])")).toHaveText([
    "Bob Abbott (bob@example.com)",
    "Alice Smith (alice@example.com)",
    "Terry Tester (tester@example.com)",
  ]);

  await picker(page).selectOption({ label: "Alice Smith (alice@example.com)" });

  await expect(page).toHaveURL(inOrg("/settings?tab=x"));
  await expect(page.locator("aside")).toContainText("Alice Smith");
  expect(api.signedInAs).toBe(2);
});
