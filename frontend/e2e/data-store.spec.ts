import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The documents panel while the user's data store is being set up, and once it's ready. */

test("documents wait for a store that's being set up, then can be uploaded", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "provisioning";
  await page.goto("/");

  const notice = page.getByRole("status", { name: "Data store" });
  await expect(notice).toContainText("Setting up your data store");
  const upload = page.getByRole("button", { name: "Upload" });
  await expect(upload).toBeDisabled();
  expect(api.writes).toEqual([]);

  api.dataStoreStatus = "ready";
  await expect(notice).toBeHidden();
  await expect(upload).toBeEnabled();
  await expect(page.getByText("Upload files to search them in chat.")).toBeVisible();

  await page.locator('input[type="file"]').setInputFiles({
    name: "notes.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Notes"),
  });
  await expect(page.getByText("notes.md")).toBeVisible();
  await expect(page.getByText("1 chunks")).toBeVisible();
});

test("uploads wait until the store's status is known", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "ready";
  api.dataStoreDelay = 1500;
  await page.goto("/");

  const upload = page.getByRole("button", { name: "Upload" });
  await upload.waitFor();
  expect(await upload.isDisabled()).toBe(true);
  await expect(upload).toBeEnabled();
});

test("a store that couldn't be set up says so", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "failed";
  await page.goto("/");

  await expect(page.getByRole("status", { name: "Data store" })).toContainText(
    "Your data store couldn't be set up",
  );
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
});

test("searching before the store is ready says it's being set up", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "provisioning";
  await page.goto("/search?q=fish");

  await expect(
    page.getByText("Your data store is being set up. Try again in a moment."),
  ).toBeVisible();
});

test("a busy store doesn't tell the chat panel there are no documents", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf");
  api.dataStoreBusy = true;
  await page.goto("/");

  await expect(page.getByRole("status", { name: "Documents list" })).toContainText(
    "Your data store is busy.",
  );
  await expect(page.getByText("Upload files to search them in chat.")).toBeHidden();

  api.dataStoreBusy = false;
  await expect(page.getByText("report.pdf")).toBeVisible();
  await expect(page.getByRole("status", { name: "Documents list" })).toBeHidden();
});
