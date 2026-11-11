import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** The Settings page: the user's own, which points to the organisation's. */

test("the Settings link opens the page, which points to the organisation's settings", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto(inOrg("/"));
  await page.getByRole("link", { name: "Settings" }).first().click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  await page.getByText("You're in").getByRole("link", { name: "Organisation" }).click();
  await expect(page).toHaveURL(/\/organisation$/);
  await expect(page.getByRole("tab", { name: /Members/ })).toHaveAttribute("aria-selected", "true");

  await page.goBack();
  await page.getByText("You're in").getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/organisation\/settings$/);
  await expect(page.getByRole("region", { name: "Approval rules" })).toBeVisible();
});
