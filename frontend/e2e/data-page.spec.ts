import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Data page: the store's Overview, and the Documents it owns. */

const overview = (page: Page) => page.getByRole("region", { name: "Data store" });
const documentList = (page: Page) => page.getByRole("list", { name: "Documents" });
const row = (page: Page, filename: string) =>
  documentList(page).getByRole("listitem").filter({ hasText: filename });
const file = (name: string) => ({
  name,
  mimeType: "text/markdown",
  buffer: Buffer.from(`# ${name}`),
});

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

test("each section has its own URL", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("plan.md");
  await page.goto("/data");

  await page.getByRole("tab", { name: "Documents" }).click();
  await expect(page).toHaveURL(/\/data\?tab=documents$/);
  await expect(row(page, "plan.md")).toBeVisible();

  // A link (or a reload) opens straight on the section.
  await page.goto("/data?tab=documents");
  await expect(page.getByRole("tab", { name: "Documents" })).toHaveAttribute("aria-selected", "true");
  await expect(row(page, "plan.md")).toBeVisible();

  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(page).toHaveURL(/\/data$/);
  await expect(overview(page)).toBeVisible();
});

test("an empty store invites an upload", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/data?tab=documents");

  await expect(page.getByText("No documents yet.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  await expect(documentList(page)).toBeHidden();
});

test("a store being set up can't take uploads until it's ready", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "provisioning";
  await page.goto("/data?tab=documents");

  await expect(page.getByRole("status", { name: "Data store" })).toContainText(
    "Setting up your data store",
  );
  const upload = page.getByRole("button", { name: "Upload" });
  await expect(upload).toBeDisabled();
  await expect(page.getByText("No documents yet")).toBeHidden();

  api.dataStoreStatus = "ready";
  await expect(upload).toBeEnabled();
  await expect(page.getByRole("status", { name: "Data store" })).toBeHidden();
  await expect(page.getByText("No documents yet")).toBeVisible();
  expect(api.writes).toEqual([]);
});

test("a store that couldn't be set up can't take uploads", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "failed";
  await page.goto("/data?tab=documents");

  await expect(page.getByRole("alert", { name: "Data store" })).toContainText(
    "Your data store couldn't be set up",
  );
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
});

test("lists each document's status, error, chunk count and upload time", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf", { chunk_count: 12 });
  api.addDocument("scan.png", { status: "failed", chunk_count: 0, error: "Unsupported image" });
  api.addDocument("slides.pptx", { status: "processing", chunk_count: 0 });
  await page.goto("/data?tab=documents");

  await expect(documentList(page).getByRole("listitem")).toHaveCount(3);
  await expect(row(page, "report.pdf")).toContainText("Ready");
  await expect(row(page, "report.pdf")).toContainText("12 chunks");
  await expect(row(page, "report.pdf")).toContainText("Uploaded Oct 14, 2026");
  await expect(row(page, "scan.png")).toContainText("Failed");
  await expect(row(page, "scan.png")).toContainText("Unsupported image");
  await expect(row(page, "slides.pptx")).toContainText("Processing");
  await expect(row(page, "slides.pptx")).not.toContainText("chunks");

  // The worker finishes, and the list follows without a reload.
  api.finishIngest("slides.pptx", { chunks: 4 });
  await expect(row(page, "slides.pptx")).toContainText("4 chunks");
  await expect(row(page, "slides.pptx")).toContainText("Ready");
});

test("an upload from the Data page shows its status until it's ready or failed", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.ingestAs = "pending";
  await page.goto("/data?tab=documents");

  await page.locator('input[type="file"]').setInputFiles([file("notes.md"), file("broken.md")]);
  await expect(row(page, "notes.md")).toContainText("Waiting");
  await expect(row(page, "broken.md")).toContainText("Waiting");
  expect(api.writes.filter(([method]) => method === "POST")).toHaveLength(2);

  api.finishIngest("notes.md", { chunks: 3 });
  api.finishIngest("broken.md", { error: "Couldn't read the file" });
  await expect(row(page, "notes.md")).toContainText("3 chunks");
  await expect(row(page, "broken.md")).toContainText("Failed");
  await expect(row(page, "broken.md")).toContainText("Couldn't read the file");
});

test("an upload from the chat appears in the Data page's list", async ({ page }) => {
  const api = await fakeApi(page);
  api.ingestAs = "pending";
  await page.goto("/");

  // The chat keeps its own upload button.
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  await page.locator('input[type="file"]').setInputFiles(file("from-chat.md"));
  await expect(page.getByText("from-chat.md")).toBeVisible();

  await page.getByRole("link", { name: "Data" }).first().click();
  await page.getByRole("tab", { name: "Documents" }).click();
  await expect(row(page, "from-chat.md")).toContainText("Waiting");
  api.finishIngest("from-chat.md", { chunks: 2 });
  await expect(row(page, "from-chat.md")).toContainText("2 chunks");
});

test("uploads still in flight when you switch tabs keep their progress and errors", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.tooLarge.add("huge.md");
  api.holdUploads();
  await page.goto("/data?tab=documents");
  await expect(page.getByText("No documents yet.")).toBeVisible();

  await page
    .locator('input[type="file"]')
    .setInputFiles([file("one.md"), file("huge.md"), file("two.md")]);
  const upload = page.getByRole("button", { name: "Upload" });
  await expect(upload.locator(".animate-spin")).toBeVisible();

  // Away and back mid-upload: it's still uploading.
  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(overview(page)).toBeVisible();
  await page.getByRole("tab", { name: "Documents" }).click();
  await expect(upload.locator(".animate-spin")).toBeVisible();

  // Away again while they finish: back on Documents, the one that failed says why.
  await page.getByRole("tab", { name: "Overview" }).click();
  api.releaseUploads();
  await expect.poll(() => api.writes.filter(([method]) => method === "POST").length).toBe(3);
  await page.getByRole("tab", { name: "Documents" }).click();
  await expect(page.getByRole("alert")).toContainText("huge.md: too large");
  await expect(row(page, "one.md")).toBeVisible();
  await expect(row(page, "two.md")).toBeVisible();
  await expect(upload.locator(".animate-spin")).toBeHidden();
});

test("an upload from the chat that finishes after you open the Data page still appears there", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.holdUploads();
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  await page.locator('input[type="file"]').setInputFiles(file("late.md"));

  // Off to the Data page before the upload answers.
  await page.getByRole("link", { name: "Data" }).first().click();
  await page.getByRole("tab", { name: "Documents" }).click();
  await expect(page.getByText("No documents yet.")).toBeVisible();

  api.releaseUploads();
  await expect(row(page, "late.md")).toBeVisible();
});

test("deleting a document removes it from the list and from search", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("keep.md");
  api.addDocument("drop.md");
  await page.goto("/search?q=drop");
  await expect(page.getByRole("region", { name: "Results" })).toContainText("drop.md");

  await page.goto("/data?tab=documents");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete drop.md" }).click();
  await expect(row(page, "drop.md")).toHaveCount(0);
  await expect(row(page, "keep.md")).toBeVisible();
  expect(api.writes).toContainEqual(["DELETE", expect.stringMatching(/^\/api\/documents\/\d+$/), null]);

  await page.goto("/search?q=drop");
  await expect(page.getByText("No passages matched.")).toBeVisible();
});

test("cancelling a delete keeps the document", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("keep.md");
  await page.goto("/data?tab=documents");

  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Delete keep.md" }).click();
  await expect(row(page, "keep.md")).toBeVisible();
  expect(api.writes).toEqual([]);
});
