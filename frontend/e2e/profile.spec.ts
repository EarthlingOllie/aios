import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Settings page's Profile section: editing your name. */

test("editing your name saves it and the sidebar shows it straight away", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");

  const profile = page.getByRole("region", { name: "Profile" });
  const first = profile.getByLabel("First name");
  const last = profile.getByLabel("Last name");
  const save = profile.getByRole("button", { name: "Save name" });
  await expect(first).toHaveValue("Terry");
  await expect(last).toHaveValue("Tester");
  await expect(page.getByText("Terry Tester")).toBeVisible();
  // Nothing to save until a name changes.
  await expect(save).toBeDisabled();

  await first.fill("Alice");
  await last.fill("Smith");
  await save.click();

  await expect(profile.getByRole("status")).toHaveText("Name saved.");
  expect(api.writes).toEqual([["PATCH", "/api/me", { first_name: "Alice", last_name: "Smith" }]]);
  // The sidebar reads the current user, which the save refreshed: no reload needed.
  await expect(page.getByText("Alice Smith")).toBeVisible();
  await expect(page.getByText("Terry Tester")).toBeHidden();
  await expect(save).toBeDisabled();

  // It's saved, not just shown.
  await page.reload();
  await expect(first).toHaveValue("Alice");
  await expect(page.getByText("Alice Smith")).toBeVisible();
});

test("an empty name is refused next to its field, and nothing changes", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");

  const profile = page.getByRole("region", { name: "Profile" });
  const last = profile.getByLabel("Last name");
  await last.fill("   ");
  await profile.getByRole("button", { name: "Save name" }).click();

  await expect(last).toHaveAttribute("aria-invalid", "true");
  await expect(profile.getByText("String should have at least 1 character")).toBeVisible();
  expect(api.accounts[0].last_name).toBe("Tester");
  await expect(page.getByText("Terry Tester")).toBeVisible();

  // Fixing it clears the error.
  await last.fill("Testing");
  await profile.getByRole("button", { name: "Save name" }).click();
  await expect(profile.getByRole("status")).toHaveText("Name saved.");
  await expect(last).not.toHaveAttribute("aria-invalid");
  await expect(page.getByText("Terry Testing")).toBeVisible();
});

test("changing your email needs your current password, then signs you in with the new one", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");

  const form = page.getByRole("form", { name: "Email" });
  await expect(form.getByText("Currently tester@example.com.")).toBeVisible();
  await form.getByLabel("New email").fill("  Terry@New.example ");
  await form.getByLabel("Current password").fill("not my password");
  await form.getByRole("button", { name: "Change email" }).click();

  // A wrong password is refused above the form: it doesn't sign you out.
  await expect(form.getByRole("alert")).toHaveText("Current password is incorrect");
  await expect(page).toHaveURL("/settings");
  expect(api.accounts[0].email).toBe("tester@example.com");

  await form.getByLabel("Current password").fill("correct horse battery");
  await form.getByRole("button", { name: "Change email" }).click();

  await expect(form.getByRole("status")).toHaveText("Email changed.");
  await expect(form.getByRole("alert")).toBeHidden();
  // The current user is refreshed: the form shows the email as the API stored it.
  await expect(form.getByText("Currently terry@new.example.")).toBeVisible();
  await expect(form.getByLabel("New email")).toHaveValue("");
  await expect(form.getByLabel("Current password")).toHaveValue("");
  expect(api.writes.at(-1)).toEqual([
    "PUT",
    "/api/me/email",
    // The email input trims it; the API lower-cases it.
    { email: "Terry@New.example", current_password: "correct horse battery" },
  ]);

  // Only the new email signs in.
  await page.locator("aside").getByRole("button", { name: "Sign out" }).click();
  await page.getByLabel("Email").fill("tester@example.com");
  await page.getByLabel("Password").fill("correct horse battery");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("Email or password is incorrect");
  await page.getByLabel("Email").fill("terry@new.example");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Terry Tester")).toBeVisible();
});

test("an email that's taken or invalid is refused, and nothing changes", async ({ page }) => {
  const api = await fakeApi(page);
  api.accounts.push({ id: 2, first_name: "Alice", last_name: "Smith", email: "alice@example.com", password: "alice's passphrase" });
  await page.goto("/settings");

  const form = page.getByRole("form", { name: "Email" });
  const email = form.getByLabel("New email");
  await email.fill("Alice@Example.com");
  await form.getByLabel("Current password").fill("correct horse battery");
  await form.getByRole("button", { name: "Change email" }).click();
  await expect(form.getByRole("alert")).toHaveText("An account with this email already exists");

  await email.fill("not an email");
  await form.getByRole("button", { name: "Change email" }).click();
  await expect(email).toHaveAttribute("aria-invalid", "true");
  await expect(form.getByText("The email address is not valid.")).toBeVisible();
  await expect(form.getByRole("alert")).toBeHidden();

  expect(api.accounts[0].email).toBe("tester@example.com");
  await expect(form.getByText("Currently tester@example.com.")).toBeVisible();
});

test("changing your password keeps you signed in here and says you're signed out elsewhere", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");

  const form = page.getByRole("form", { name: "Password" });
  const current = form.getByLabel("Current password");
  const next = form.getByLabel("New password", { exact: true });
  const confirm = form.getByLabel("Confirm new password");
  await current.fill("correct horse battery");
  await next.fill("a brand new passphrase");
  await confirm.fill("a brand new passphrase");
  await form.getByRole("button", { name: "Change password" }).click();

  await expect(form.getByRole("status")).toHaveText("Password changed. You've been signed out everywhere else.");
  expect(api.writes).toEqual([
    [
      "PUT",
      "/api/me/password",
      {
        current_password: "correct horse battery",
        new_password: "a brand new passphrase",
        new_password_confirm: "a brand new passphrase",
      },
    ],
  ]);
  expect(api.accounts[0].password).toBe("a brand new passphrase");
  for (const field of [current, next, confirm]) await expect(field).toHaveValue("");

  // Still signed in here.
  await page.reload();
  await expect(page.getByText("Terry Tester")).toBeVisible();
  await expect(page).toHaveURL("/settings");
});

test("a new password that doesn't match, is too short or comes with a wrong current one is refused", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");

  const form = page.getByRole("form", { name: "Password" });
  const current = form.getByLabel("Current password");
  const next = form.getByLabel("New password", { exact: true });
  const confirm = form.getByLabel("Confirm new password");
  const submit = form.getByRole("button", { name: "Change password" });

  // A mismatch is said on leaving the confirmation, and stops the request.
  await current.fill("correct horse battery");
  await next.fill("a brand new passphrase");
  await confirm.fill("a different passphrase");
  await confirm.blur();
  await expect(form.getByText("Passwords don't match")).toBeVisible();
  await submit.click();
  await expect(confirm).toHaveAttribute("aria-invalid", "true");
  expect(api.writes).toEqual([]);

  await next.fill("too short");
  await confirm.fill("too short");
  await submit.click();
  await expect(next).toHaveAttribute("aria-invalid", "true");
  await expect(form.getByText("Password must be at least 12 characters")).toBeVisible();
  await expect(form.getByText("Passwords don't match")).toBeHidden();

  await current.fill("not my password");
  await next.fill("a brand new passphrase");
  await confirm.fill("a brand new passphrase");
  await submit.click();
  await expect(form.getByRole("alert")).toHaveText("Current password is incorrect");
  await expect(page).toHaveURL("/settings");

  expect(api.accounts[0].password).toBe("correct horse battery");
});

test("too many wrong current passwords say to try again later", async ({ page }) => {
  const api = await fakeApi(page);
  api.authThrottled = true;
  await page.goto("/settings");

  const email = page.getByRole("form", { name: "Email" });
  await email.getByLabel("New email").fill("terry@new.example");
  await email.getByLabel("Current password").fill("correct horse battery");
  await email.getByRole("button", { name: "Change email" }).click();
  await expect(email.getByRole("alert")).toHaveText("Too many attempts. Try again later.");

  const password = page.getByRole("form", { name: "Password" });
  await password.getByLabel("Current password").fill("correct horse battery");
  await password.getByLabel("New password", { exact: true }).fill("a brand new passphrase");
  await password.getByLabel("Confirm new password").fill("a brand new passphrase");
  await password.getByRole("button", { name: "Change password" }).click();
  await expect(password.getByRole("alert")).toHaveText("Too many attempts. Try again later.");

  expect(api.accounts[0]).toMatchObject({ email: "tester@example.com", password: "correct horse battery" });
});
