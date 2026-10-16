import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Data page: the store's Overview, and the Documents it owns. */

test("Data is in the navigation and opens on the Overview", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/");

  await page.getByRole("link", { name: "Data" }).first().click();

  await expect(page).toHaveURL(/\/data$/);
  await expect(page.getByRole("heading", { name: "Data", level: 1 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  const store = page.getByRole("region", { name: "Data store" });
  await expect(store).toContainText("Ready");
  await expect(store).toContainText("Your data store is ready.");
});
