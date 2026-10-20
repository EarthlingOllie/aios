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
