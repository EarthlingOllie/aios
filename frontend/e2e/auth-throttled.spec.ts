import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** Signing in and registering past the API's rate limits (429) say to try again later. */

test("too many sign-in attempts say to try again later", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;
  api.authThrottled = true;

  await page.goto("/login");
  await page.getByLabel("Email").fill("tester@example.com");
  await page.getByLabel("Password").fill("correct horse battery");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByRole("alert")).toHaveText("Too many attempts. Try again later.");
  await expect(page).toHaveURL("/login");
  expect(api.signedInAs).toBeNull();
});

test("too many registrations say to try again later", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;
  api.authThrottled = true;

  await page.goto("/register");
  await page.getByLabel("First name").fill("Alice");
  await page.getByLabel("Last name").fill("Smith");
  await page.getByLabel("Email").fill("alice@example.com");
  await page.getByLabel("Password", { exact: true }).fill("a long passphrase");
  await page.getByLabel("Confirm password").fill("a long passphrase");
  await page.getByRole("button", { name: "Create account" }).click();

  await expect(page.getByRole("alert")).toHaveText("Too many attempts. Try again later.");
  await expect(page).toHaveURL("/register");
  expect(api.accounts.map((a) => a.email)).not.toContain("alice@example.com");
});
