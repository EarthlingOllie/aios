import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Data page: the store's Overview, and the Documents it owns. */

const overview = (page: Page) => page.getByRole("region", { name: "Data store" });
const documentList = (page: Page) => page.getByRole("list", { name: "Documents" });
const row = (page: Page, filename: string) =>
  documentList(page).getByRole("listitem").filter({ hasText: filename });
/** When the tests that drive the page's clock start it. */
const START = new Date("2026-10-15T09:00:00Z");
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
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
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

test("Documents and the chat's panel say when the store's status can't be read", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.dataStoreFails = true;
  await page.goto("/data?tab=documents");

  const notice = page.getByRole("status", { name: "Data store" });
  await expect(notice).toContainText("Couldn't check your data store");
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();

  await page.getByRole("link", { name: "Chat" }).first().click();
  await expect(notice).toContainText("Couldn't check your data store");

  api.dataStoreFails = false;
  await expect(notice).toBeHidden({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
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
  await expect(page.getByRole("tab", { name: "Documents" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
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

test("lists each document's status, error, chunk count, size and upload time", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf", { chunk_count: 12, size_bytes: 2.5 * 1024 * 1024 });
  api.addDocument("scan.png", { status: "failed", chunk_count: 0, error: "Unsupported image" });
  api.addDocument("slides.pptx", { status: "processing", chunk_count: 0 });
  await page.goto("/data?tab=documents");

  await expect(documentList(page).getByRole("listitem")).toHaveCount(3);
  await expect(row(page, "report.pdf")).toContainText("Ready");
  await expect(row(page, "report.pdf")).toContainText("12 chunks");
  await expect(row(page, "report.pdf")).toContainText("Uploaded Oct 14, 2026");
  await expect(row(page, "report.pdf")).toContainText("2.5 MB");
  await expect(row(page, "scan.png")).toContainText("Failed");
  await expect(row(page, "scan.png")).toContainText("Unsupported image");
  await expect(row(page, "slides.pptx")).toContainText("Processing");
  await expect(row(page, "slides.pptx")).not.toContainText("chunks");

  // The worker finishes, and the list follows without a reload.
  api.finishIngest("slides.pptx", { chunks: 4 });
  await expect(row(page, "slides.pptx")).toContainText("4 chunks");
  await expect(row(page, "slides.pptx")).toContainText("Ready");
});

test("each document links to its download", async ({ page }) => {
  const api = await fakeApi(page);
  const doc = api.addDocument("Résumé (final), 2026.pdf");
  await page.goto("/data?tab=documents");

  const link = row(page, doc.filename).getByRole("link", { name: `Download ${doc.filename}` });
  // The API's address, never a file's link itself: the API mints one per click, which saves
  // the file under its name. A download (not a navigation), so a refusal leaves the page be.
  // (The browser following the API's redirect to the file is tested against the real app.)
  await expect(link).toHaveAttribute("href", `/api/documents/${doc.id}/download`);
  await expect(link).toHaveAttribute("download");
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

test("an upload from the chat that lands after you open the Data page appears there", async ({
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

test("a second upload waits for the one in flight to finish", async ({ page }) => {
  const api = await fakeApi(page);
  api.holdUploads();
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles(file("first.md"));

  // The chat's upload is still going: the Data page's button waits for it, so the two can't
  // trample each other's progress and errors.
  await page.getByRole("link", { name: "Data" }).first().click();
  await page.getByRole("tab", { name: "Documents" }).click();
  const upload = page.getByRole("button", { name: "Upload" });
  await expect(upload).toBeDisabled();
  await expect(upload.locator(".animate-spin")).toBeVisible();

  api.releaseUploads();
  await expect(row(page, "first.md")).toBeVisible();
  await expect(upload).toBeEnabled();
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
  expect(api.writes).toContainEqual([
    "DELETE",
    expect.stringMatching(/^\/api\/documents\/\d+$/),
    null,
  ]);

  await page.goto("/search?q=drop");
  await expect(page.getByText("No passages matched.")).toBeVisible();
});

test("opening Documents reads the list again, picking up changes made elsewhere", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addDocument("old.md");
  await page.goto("/");
  await expect(page.getByText("old.md")).toBeVisible();

  // Another tab (or an agent) adds a document while this one sits on the chat.
  api.addDocument("elsewhere.md");
  await page.getByRole("link", { name: "Data" }).first().click();
  await page.getByRole("tab", { name: "Documents" }).click();
  await expect(row(page, "elsewhere.md")).toBeVisible();

  // And back on the chat, its panel reads it again too.
  api.addDocument("later.md");
  await page.getByRole("link", { name: "Chat" }).first().click();
  await expect(page.getByText("later.md")).toBeVisible();
});

test("a store that stops being ready is noticed on opening the Data or Search page", async ({
  page,
}) => {
  const api = await fakeApi(page);
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();

  // Nothing polls a ready store, so only opening a page that shows it finds out.
  api.dataStoreStatus = "failed";
  await page.getByRole("link", { name: "Data" }).first().click();
  await expect(overview(page)).toContainText("Your data store couldn't be set up.");

  await page.getByRole("link", { name: "Search" }).first().click();
  await expect(page.getByRole("alert", { name: "Data store" })).toContainText(
    "Your data store couldn't be set up",
  );
});

test("a search that matches nothing points to the Data page's Documents", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/search?q=fish");
  await expect(page.getByText("No passages matched.")).toBeVisible();

  await page.getByRole("link", { name: "Data page" }).click();
  await expect(page).toHaveURL(/\/data\?tab=documents$/);
  await expect(page.getByText("No documents yet.")).toBeVisible();
});

test("deleting a document twice, before the list catches up, isn't an error", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addDocument("drop.md");
  await page.goto("/data?tab=documents");
  await expect(row(page, "drop.md")).toBeVisible();

  // The list is slow to answer after the delete, so the row stays up to be clicked again.
  api.holdDocumentLists();
  page.on("dialog", (dialog) => dialog.accept());
  const remove = page.getByRole("button", { name: "Delete drop.md" });
  await remove.click();
  await expect.poll(() => api.heldDocumentLists.length).toBe(1);
  await remove.click();
  await expect
    .poll(() => api.writes.filter(([method]) => method === "DELETE").length)
    .toBeGreaterThan(0);

  api.holdDocumentLists(false);
  for (const release of api.heldDocumentLists) release();
  await expect(row(page, "drop.md")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
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

test("a slow list shows it's loading, not that there are no documents", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf");
  api.documentsDelay = 1500;
  await page.goto("/data?tab=documents");

  await expect(page.getByLabel("Loading documents")).toBeVisible();
  await expect(page.getByText("No documents yet")).toBeHidden();

  await expect(row(page, "report.pdf")).toBeVisible();
  await expect(page.getByLabel("Loading documents")).toBeHidden();
});

test("a busy store says so, and the list loads once it's free", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf");
  api.dataStoreBusy = true;
  await page.goto("/data?tab=documents");

  await expect(page.getByRole("status", { name: "Documents list" })).toContainText(
    "Your data store is busy.",
  );
  await expect(page.getByText("No documents yet.")).toBeHidden();

  api.dataStoreBusy = false;
  await expect(row(page, "report.pdf")).toBeVisible();
  await expect(page.getByRole("status", { name: "Documents list" })).toBeHidden();
});

test("an upload refused by a busy store says so", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/data?tab=documents");
  await expect(page.getByText("No documents yet.")).toBeVisible();

  api.dataStoreBusy = true;
  await page.locator('input[type="file"]').setInputFiles(file("notes.md"));
  await expect(page.getByRole("alert")).toContainText(
    "notes.md: Your data store is busy. Try again in a moment.",
  );
});

test("a list refused for good says so once, without asking again", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("report.pdf");
  await page.goto("/data?tab=documents");
  await expect(row(page, "report.pdf")).toBeVisible();

  // The session expires: the next read of the list is refused, and so would every later one be.
  let reads = 0;
  await page.route("**/api/documents", (route) => {
    reads++;
    return route.fulfill({ status: 401, body: '{"detail":"Not authenticated"}' });
  });
  await page.locator('input[type="file"]').setInputFiles(file("notes.md"));

  const notice = page.getByRole("status", { name: "Documents list" });
  await expect(notice).toContainText(
    "Couldn't load your documents (session expired, sign in again).",
  );
  await expect(notice).not.toContainText("Trying again");
  const after = reads;
  await page.waitForTimeout(5000);
  expect(reads).toBe(after);
});

test("a list refused for good stops polling, even with an ingest still running", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addDocument("slides.pptx", { status: "processing", chunk_count: 0 });
  await page.goto("/data?tab=documents");
  await expect(row(page, "slides.pptx")).toContainText("Processing");

  let reads = 0;
  await page.route("**/api/documents", (route) => {
    reads++;
    return route.fulfill({ status: 401, body: '{"detail":"Not authenticated"}' });
  });
  const notice = page.getByRole("status", { name: "Documents list" });
  await expect(notice).toContainText("session expired, sign in again");
  const after = reads;
  await page.waitForTimeout(5000);
  expect(reads).toBe(after);
});

test("a slow poll that answers after a delete doesn't bring the document back", async ({
  page,
}) => {
  const api = await fakeApi(page);
  // Something still ingesting keeps the list polling.
  api.addDocument("slides.pptx", { status: "processing", chunk_count: 0 });
  api.addDocument("drop.md");
  // Timers only move when the test says, so no other poll can hide (or cause) a stale list.
  await page.clock.install({ time: START });
  await page.clock.pauseAt(START);
  await page.goto("/data?tab=documents");
  await expect(row(page, "drop.md")).toBeVisible();

  // A poll reads the list (drop.md still in it), but its answer is slow to arrive.
  api.holdDocumentLists();
  await page.clock.runFor(2000);
  await expect.poll(() => api.heldDocumentLists.length).toBe(1);
  api.holdDocumentLists(false);

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete drop.md" }).click();
  await expect(row(page, "drop.md")).toHaveCount(0);

  // The slow poll answers last, with the list from before the delete.
  const stale = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/documents");
  api.heldDocumentLists[0]();
  await stale;
  await page.waitForTimeout(300);
  await expect(row(page, "drop.md")).toHaveCount(0);
  await expect(row(page, "slides.pptx")).toBeVisible();
});

test("a slow list doesn't stack up polls behind it", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("slides.pptx", { status: "processing", chunk_count: 0 });
  await page.clock.install({ time: START });
  await page.clock.pauseAt(START);
  await page.goto("/data?tab=documents");
  await expect(row(page, "slides.pptx")).toBeVisible();

  // The store is slow to answer: while a read is out, no more are sent.
  api.holdDocumentLists();
  await page.clock.runFor(10_000);
  await expect.poll(() => api.heldDocumentLists.length).toBe(1);
  await page.waitForTimeout(300);
  expect(api.heldDocumentLists).toHaveLength(1);

  // Once it answers, polling carries on.
  api.holdDocumentLists(false);
  api.heldDocumentLists[0]();
  api.finishIngest("slides.pptx", { chunks: 4 });
  await page.clock.runFor(10_000);
  await expect(row(page, "slides.pptx")).toContainText("4 chunks");
});
