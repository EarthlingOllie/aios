import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Data page: the store's Overview, and the Documents it owns. */

const overview = (page: Page) => page.getByRole("region", { name: "Data store" });

test("Data is in the navigation and opens on the Overview", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/");

  await page.getByRole("link", { name: "Data" }).first().click();

  await expect(page).toHaveURL(/\/data$/);
  await expect(page.getByRole("heading", { name: "Data", level: 1 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  await expect(overview(page)).toContainText("Ready");
  await expect(overview(page)).toContainText("Your data store is ready.");
});

test("the Overview says a store is being set up, until it's ready", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "provisioning";
  await page.goto("/data");

  await expect(overview(page)).toContainText("Being set up");
  await expect(overview(page)).toContainText("We're setting up your data store.");

  api.dataStoreStatus = "ready";
  await expect(overview(page)).toContainText("Your data store is ready.");
});

test("the Overview says a store couldn't be set up", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "failed";
  await page.goto("/data");

  await expect(overview(page)).toContainText("Failed");
  await expect(overview(page)).toContainText("Your data store couldn't be set up.");
});

test("a store whose status can't be read says so, and recovers when it can", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreFails = true;
  await page.goto("/data");

  await expect(overview(page)).toContainText("Couldn't check your data store");
  await expect(overview(page)).toContainText("Trying again…");

  // It keeps asking, so the page catches up once the server answers again.
  api.dataStoreFails = false;
  await expect(overview(page)).toContainText("Your data store is ready.", { timeout: 10_000 });
  await expect(overview(page)).not.toContainText("Couldn't check your data store");
});
