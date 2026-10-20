import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The documents panel while the organisation's data store is being set up, and once it's ready. */

test("documents wait for a store that's being set up, then can be uploaded", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "provisioning";
  await page.goto("/");

  const notice = page.getByRole("status", { name: "Data store" });
  await expect(notice).toContainText("Setting up the organisation's data store");
  const upload = page.getByRole("button", { name: "Upload" });
  await expect(upload).toBeDisabled();
  expect(api.writes).toEqual([]);
  // Nothing to list until the store is ready: asking would only be refused.
  expect(api.documentLists).toBe(0);

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

test("a ready store's documents are listed once", async ({ page }) => {
  const api = await fakeApi(page);
  api.documents.push({
    id: 1,
    filename: "notes.md",
    status: "ready",
    error: null,
    chunk_count: 3,
    size_bytes: 7,
    uploaded_by: null,
    created_at: "2026-10-14T09:00:00Z",
  });
  await page.goto("/");

  await expect(page.getByText("notes.md")).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  // Give a second request, if one were coming, time to arrive.
  await page.waitForTimeout(500);
  expect(api.documentLists).toBe(1);
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

  // An error, not news of progress.
  await expect(page.getByRole("alert", { name: "Data store" })).toContainText(
    "The organisation's data store couldn't be set up",
  );
  await expect(page.getByRole("status", { name: "Data store" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
});

test("a busy store doesn't tell the chat panel there are no documents", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf");
  api.dataStoreBusy = true;
  await page.goto("/");

  await expect(page.getByRole("status", { name: "Documents list" })).toContainText(
    "The organisation's data store is busy.",
  );
  await expect(page.getByText("Upload files to search them in chat.")).toBeHidden();

  api.dataStoreBusy = false;
  await expect(page.getByText("report.pdf")).toBeVisible();
  await expect(page.getByRole("status", { name: "Documents list" })).toBeHidden();
});

test("the chat's panel shows it's loading, not an invitation to upload", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf");
  api.documentsDelay = 1500;
  await page.goto("/");

  await expect(page.getByLabel("Loading documents")).toBeVisible();
  await expect(page.getByText("Upload files to search them in chat.")).toBeHidden();

  await expect(page.getByText("report.pdf")).toBeVisible();
  await expect(page.getByLabel("Loading documents")).toBeHidden();
});

test("search waits for a store that's being set up, then searches", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "provisioning";
  await page.goto("/search?q=fish");

  // News of progress, not a failed search.
  await expect(page.getByRole("status", { name: "Data store" })).toContainText(
    "Setting up the organisation's data store",
  );
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Search" })).toBeDisabled();

  api.dataStoreStatus = "ready";
  await expect(page.getByRole("status", { name: "Data store" })).toBeHidden();
  await expect(page.getByText("No passages matched.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Search" })).toBeEnabled();
});

test("search says when a store couldn't be set up", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "failed";
  await page.goto("/search?q=fish");

  await expect(page.getByRole("alert", { name: "Data store" })).toContainText(
    "The organisation's data store couldn't be set up",
  );
  await expect(page.getByText(/Search failed/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Search" })).toBeDisabled();
});
