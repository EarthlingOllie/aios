import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** Registering, signing in and out, and coming back to a deep link after signing in. */

const sidebar = (page: Page) => page.locator("aside");

async function signIn(page: Page, email: string, password: string) {
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("a deep link while signed out comes back to that page after signing in", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;

  await page.goto("/settings?tab=x");
  await expect(page).toHaveURL("/login?next=%2Fsettings%3Ftab%3Dx");
  await signIn(page, "Tester@Example.com", "correct horse battery");

  await expect(page).toHaveURL("/settings?tab=x");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(sidebar(page)).toContainText("Terry Tester");
});

test("a next pointing off-site is ignored", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;

  await page.goto("/login?next=https%3A%2F%2Fevil.example%2F");
  await signIn(page, "tester@example.com", "correct horse battery");

  await expect(sidebar(page)).toContainText("Terry Tester");
  await expect(page).not.toHaveURL(/evil/);
  await expect(page).not.toHaveURL(/\/login/);
});

test("a wrong password is refused above the form", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;

  await page.goto("/login");
  await signIn(page, "tester@example.com", "not the password");

  await expect(page.getByRole("alert")).toHaveText("Email or password is incorrect");
  await expect(page).toHaveURL("/login");
});

test("signing out ends the session", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");
  await expect(sidebar(page)).toContainText("Terry Tester");

  await sidebar(page).getByRole("button", { name: "Sign out" }).click();

  await expect(page).toHaveURL("/login");
  expect(api.signedInAs).toBeNull();
  await page.goto("/settings");
  await expect(page).toHaveURL("/login?next=%2Fsettings");
});

test("signed in, the sign-in and registration pages go to the app", async ({ page }) => {
  await fakeApi(page);
  for (const path of ["/login", "/register"]) {
    await page.goto(path);
    await expect(sidebar(page)).toContainText("Terry Tester");
    await expect(page).not.toHaveURL(/\/(login|register)/);
  }
});

test("a new visitor registers and is signed in", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;
  api.authDelay = 300;

  await page.goto("/chats");
  await page.getByRole("link", { name: "Create an account" }).click();
  await expect(page).toHaveURL("/register?next=%2Fchats");

  await page.getByLabel("First name").fill(" Alice ");
  await page.getByLabel("Last name").fill("Smith");
  await page.getByLabel("Email").fill("alice@example.com");
  await page.getByLabel("Password", { exact: true }).fill("a long passphrase");
  // A mismatch shows once the confirmation is left, and again on submit, which sends nothing.
  await page.getByLabel("Confirm password").fill("a long passphrasf");
  await page.getByLabel("Confirm password").blur();
  await expect(page.getByText("Passwords don't match")).toBeVisible();
  await page.getByLabel("Confirm password").fill("a long passphrase");
  await expect(page.getByText("Passwords don't match")).toBeHidden();
  await page.getByLabel("Confirm password").fill("another passphrase");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("Passwords don't match")).toBeVisible();
  expect(api.writes).toEqual([]);

  await page.getByLabel("Confirm password").fill("a long passphrase");
  const create = page.getByRole("button", { name: "Create account" });
  await create.click();
  // One request at a time.
  await expect(create).toBeDisabled();

  await expect(page).toHaveURL("/chats");
  await expect(sidebar(page)).toContainText("Alice Smith");
  expect(api.writes).toEqual([
    [
      "POST",
      "/api/auth/register",
      {
        first_name: " Alice ",
        last_name: "Smith",
        email: "alice@example.com",
        password: "a long passphrase",
        password_confirm: "a long passphrase",
      },
    ],
  ]);
});

test("registration shows a taken email above the form and an invalid field next to it", async ({ page }) => {
  const api = await fakeApi(page);
  api.signedInAs = null;

  await page.goto("/register");
  await page.getByLabel("First name").fill("Terry");
  await page.getByLabel("Last name").fill("Tester");
  await page.getByLabel("Email").fill("TESTER@example.com");
  await page.getByLabel("Password", { exact: true }).fill("short");
  await page.getByLabel("Confirm password").fill("short");
  await page.getByRole("button", { name: "Create account" }).click();

  await expect(page.getByText("Password must be at least 12 characters")).toBeVisible();
  await expect(page.getByLabel("Password", { exact: true })).toHaveAttribute("aria-invalid", "true");

  await page.getByLabel("Password", { exact: true }).fill("a long passphrase");
  await page.getByLabel("Confirm password").fill("a long passphrase");
  await page.getByRole("button", { name: "Create account" }).click();

  await expect(page.getByRole("alert")).toHaveText("An account with this email already exists");
  await expect(page.getByText("Password must be at least 12 characters")).toBeHidden();

  await page.getByRole("link", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/login");
});

test("signed out elsewhere, the next request goes to sign in and back afterwards", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");
  await expect(sidebar(page)).toContainText("Terry Tester");

  // As if the password was changed in another browser: this session is no longer valid.
  api.signedInAs = null;
  await sidebar(page).getByRole("link", { name: "Agents" }).click();

  await expect(page).toHaveURL("/login?next=%2Fagents");
  await signIn(page, "tester@example.com", "correct horse battery");
  await expect(page).toHaveURL("/agents");
  await expect(sidebar(page)).toContainText("Terry Tester");
});
