import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Sources on the Data page: the Sources tab listing every source with its health and last sync,
 * Uploads with no way to pause or remove it; a source's page with its recent syncs, Sync now, Full
 * resync, pause and resume (admins only, disabled with the reason for others); adding a source;
 * each document's source on the Documents tab, filterable by source, with no delete for a synced
 * one; and flagged synced items on the flagged list.
 */

const sourceList = (page: Page) => page.getByRole("list", { name: "Sources" });
const documentList = (page: Page) => page.getByRole("list", { name: "Documents" });
const row = (page: Page, filename: string) =>
  documentList(page).getByRole("listitem").filter({ hasText: filename });

for (const role of ["admin", "participant"] as const) {
  test(`the Sources tab lists Uploads for ${role === "admin" ? "an admin" : "a participant"}`, async ({
    page,
  }) => {
    const api = await fakeApi(page);
    api.actAs(role);
    api.addDocument("plan.md");
    await page.goto(inOrg("/data"));

    await page.getByRole("tab", { name: "Sources" }).click();

    await expect(page).toHaveURL(/\/data\/sources$/);
    const uploads = sourceList(page).getByRole("listitem").filter({ hasText: "Uploads" });
    await expect(uploads).toBeVisible();
    await expect(uploads).toContainText("Healthy");
    await expect(uploads).toContainText("1 document");
    await expect(sourceList(page).getByRole("listitem")).toHaveCount(1);
    // Uploads is always on: nothing offers to pause or remove it.
    await expect(sourceList(page).getByRole("button")).toHaveCount(0);
  });
}

test("each document shows its source, and the list filters by source", async ({ page }) => {
  const api = await fakeApi(page);
  const prices = api.addSource("Price page", "url");
  api.addDocument("plan.md");
  api.addDocument("prices.md", { source: prices });
  await page.goto(inOrg("/data/documents"));

  await expect(row(page, "plan.md")).toContainText("Uploads");
  await expect(row(page, "prices.md")).toContainText("Price page");

  const filter = page.getByRole("combobox", { name: "Source" });
  await filter.selectOption({ label: "Price page" });
  await expect(page).toHaveURL(/\/data\/documents\?source=\d+$/);
  await expect(row(page, "prices.md")).toBeVisible();
  await expect(row(page, "plan.md")).toBeHidden();

  // The filter is in the URL, so a reload keeps it.
  await page.reload();
  await expect(filter).toHaveValue(String(prices.id));
  await expect(row(page, "plan.md")).toBeHidden();

  await filter.selectOption({ label: "Uploads" });
  await expect(row(page, "plan.md")).toBeVisible();
  await expect(row(page, "prices.md")).toBeHidden();

  await filter.selectOption({ label: "All sources" });
  await expect(row(page, "plan.md")).toBeVisible();
  await expect(row(page, "prices.md")).toBeVisible();
});

test("an upload lands in Uploads", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/data/documents"));

  await page.locator('input[type="file"]').setInputFiles({
    name: "notes.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Notes"),
  });

  await expect(row(page, "notes.md")).toContainText("Uploads");
});

test("the Sources tab shows each source's health and last sync", async ({ page }) => {
  const api = await fakeApi(page);
  api.addSource("Wiki", "connector", { item_count: 4, syncs: [api.sync({ added: 3, updated: 1 })] });
  api.addSource("Tickets", "connector", { status: "failing", health: "failing" });
  api.addSource("Drive", "connector", { status: "needs_reauth", health: "needs_reconnecting" });
  api.addSource("Feed", "connector", { health: "delayed" });
  await page.goto(inOrg("/data/sources"));

  const item = (name: string) => sourceList(page).getByRole("listitem").filter({ hasText: name });
  await expect(item("Wiki")).toContainText("Healthy");
  await expect(item("Wiki")).toContainText("4 items");
  await expect(item("Wiki")).toContainText("3 added · 1 updated");
  await expect(item("Wiki")).toContainText("syncs every 15 minutes");
  await expect(item("Tickets")).toContainText("Failing");
  await expect(item("Drive")).toContainText("Needs reconnecting");
  await expect(item("Feed")).toContainText("Delayed");
});

test("an admin adds a source, and it's listed", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/data/sources"));

  await page.getByRole("button", { name: "Add source" }).click();
  const form = page.getByRole("form", { name: "Add source" });
  await form.getByLabel("Kind").selectOption({ label: "Test provider" });
  await form.getByLabel("Name").fill("Team wiki");
  await form.getByLabel("Sync every (minutes)").fill("2");
  await form.getByRole("button", { name: "Add" }).click();
  await expect(form.getByRole("alert")).toContainText("at most every 5 minutes");

  await form.getByLabel("Sync every (minutes)").fill("30");
  await form.getByRole("button", { name: "Add" }).click();

  await expect(form).toBeHidden();
  const added = sourceList(page).getByRole("listitem").filter({ hasText: "Team wiki" });
  await expect(added).toContainText("syncs every 30 minutes");
  expect(api.sources.find((s) => s.name === "Team wiki")?.interval_seconds).toBe(1800);
});

test("a source's page lists its recent syncs, and an admin syncs, resyncs, pauses and resumes it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const wiki = api.addSource("Wiki", "connector", {
    syncs: [
      api.sync({ status: "failed", error: "The test feed is down." }),
      api.sync({ kind: "full", added: 2, skipped: 3, storage_full: true, error: "Storage full: new items were skipped until there's room." }),
    ],
  });
  await page.goto(inOrg("/data/sources"));
  await sourceList(page).getByRole("link", { name: "Wiki" }).click();

  await expect(page).toHaveURL(new RegExp(`/data/sources/${wiki.id}$`));
  const syncs = page.getByRole("list", { name: "Syncs" }).getByRole("listitem");
  await expect(syncs.first()).toContainText("Failed");
  await expect(syncs.first()).toContainText("The test feed is down.");
  await expect(syncs.nth(1)).toContainText("2 added · 3 skipped (storage full)");

  await page.getByRole("button", { name: "Sync now" }).click();
  await expect(syncs).toHaveCount(3);
  await page.getByRole("button", { name: "Full resync" }).click();
  await expect(syncs).toHaveCount(4);
  expect(api.syncRequests).toEqual([
    { id: wiki.id, full: false },
    { id: wiki.id, full: true },
  ]);

  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByRole("button", { name: "Resume" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sync now" })).toBeDisabled();
  expect(wiki.status).toBe("paused");
  await page.getByRole("button", { name: "Resume" }).click();
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
  expect(wiki.status).toBe("active");
});

for (const role of ["editor", "participant"] as const) {
  test(`${role === "editor" ? "an editor" : "a participant"} sees source actions disabled, with why`, async ({
    page,
  }) => {
    const api = await fakeApi(page);
    api.actAs(role);
    const wiki = api.addSource("Wiki", "connector");
    await page.goto(inOrg("/data/sources"));

    await expect(page.getByRole("button", { name: "Add source" })).toBeDisabled();
    await expect(page.getByRole("note", { name: "What your role allows" })).toContainText(
      "add, change, sync, pause or remove sources",
    );
    await page.goto(inOrg(`/data/sources/${wiki.id}`));
    for (const name of ["Sync now", "Full resync", "Pause"]) {
      await expect(page.getByRole("button", { name })).toBeDisabled();
    }
    await expect(page.getByRole("note", { name: "What your role allows" })).toBeVisible();
    expect(api.syncRequests).toEqual([]);
  });
}

test("Uploads' page offers no sync, pause or resume", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/data/sources/1"));

  await expect(page.getByRole("heading", { name: /Uploads/ })).toBeVisible();
  await expect(page.getByText("Uploads doesn't sync")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sync now" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Pause" })).toHaveCount(0);
});

test("a synced document can't be deleted on the Documents tab; an upload can", async ({ page }) => {
  const api = await fakeApi(page);
  const wiki = api.addSource("Wiki", "connector");
  api.addDocument("plan.md");
  api.addDocument("synced.md", { source: wiki, url: "https://wiki.example/synced" });
  await page.goto(inOrg("/data/documents"));

  await expect(row(page, "synced.md")).toContainText("Synced");
  await expect(row(page, "synced.md").getByRole("link", { name: "Open at its source" })).toHaveAttribute(
    "href",
    "https://wiki.example/synced",
  );
  await expect(page.getByRole("button", { name: "Delete synced.md" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete plan.md" })).toHaveCount(1);
});

test("an admin releases a flagged synced item from the flagged list", async ({ page }) => {
  const api = await fakeApi(page);
  const wiki = api.addSource("Wiki", "connector");
  api.flaggedItems.push({
    id: 41,
    title: "Lobsters",
    url: "https://wiki.example/lobsters",
    source_id: wiki.id,
    source_name: "Wiki",
    reason: "Jev judged this a likely prompt injection (probability 0.97).",
    checked_at: null,
    created_at: "2026-11-12T10:00:00Z",
  });
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto(inOrg("/organisation/settings"));

  const items = page.getByRole("list", { name: "Flagged synced items" }).getByRole("listitem");
  await expect(items).toHaveCount(1);
  await expect(items.first()).toContainText("Synced from Wiki");
  await expect(items.first()).toContainText("likely prompt injection");
  await page.getByRole("button", { name: "Release Lobsters" }).click();

  await expect(page.getByRole("region", { name: "Flagged content" })).toContainText("Nothing is flagged.");
  expect(api.flaggedItems[0].released).toBe(true);
});

test("an admin adds a web page, and an address aios can't reach is refused with why", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/data/sources"));

  await page.getByRole("button", { name: "Add source" }).click();
  const form = page.getByRole("form", { name: "Add source" });
  await expect(form.getByLabel("Kind")).toHaveValue("url");
  await form.getByLabel("Name").fill("Price page");
  await form.getByLabel("URL").fill("http://169.254.169.254/latest/meta-data");
  await form.getByRole("button", { name: "Add" }).click();
  await expect(form.getByRole("alert")).toContainText("This address isn't reachable from aios");

  await form.getByLabel("URL").fill("https://example.com/prices");
  await form.getByRole("button", { name: "Add" }).click();

  await expect(form).toBeHidden();
  const added = sourceList(page).getByRole("listitem").filter({ hasText: "Price page" });
  await expect(added).toContainText("Web page");
  expect(api.sources.find((s) => s.name === "Price page")?.url).toBe("https://example.com/prices");

  await added.getByRole("link", { name: "Price page" }).click();
  await expect(page.getByRole("link", { name: "https://example.com/prices" })).toBeVisible();
});

test("an admin adds a webhook: its secret is shown once, and a new one replaces it", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/data/sources"));

  await page.getByRole("button", { name: "Add source" }).click();
  const form = page.getByRole("form", { name: "Add source" });
  await form.getByLabel("Kind").selectOption({ label: "Webhook" });
  await expect(form.getByLabel("Sync every (minutes)")).toBeHidden();
  await form.getByLabel("Name").fill("Orders");
  await form.getByRole("button", { name: "Add" }).click();

  const shown = page.getByRole("region", { name: "Shown once" });
  await expect(shown.getByLabel("Signing secret")).toHaveValue("whsec_secret1");
  await expect(shown.getByLabel("Webhook URL")).toHaveValue(/\/api\/webhooks\/token1$/);
  await expect(shown).toContainText("won't be shown again");
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(shown).toBeHidden();

  const added = sourceList(page).getByRole("listitem").filter({ hasText: "Orders" });
  await expect(added).toContainText("Webhook");
  await added.getByRole("link", { name: "Orders" }).click();
  // Its URL stays on its page; its secret doesn't. It has nothing to sync.
  await expect(page.getByLabel("Webhook URL")).toHaveValue(/token1$/);
  await expect(page.getByText("whsec_secret1")).toBeHidden();
  await expect(page.getByRole("button", { name: "Sync now" })).toHaveCount(0);
  await expect(page.getByText("Nothing has arrived yet.")).toBeVisible();

  await page.getByRole("button", { name: "New secret" }).click();
  const rotated = page.getByRole("region", { name: "Shown once" });
  await expect(rotated.getByLabel("Signing secret")).toHaveValue("whsec_secret2");
  await rotated.getByRole("button", { name: "Done" }).click();
  await expect(page.getByLabel("Webhook URL")).toHaveValue(/token2$/);
});

test("an admin adds an email source: its address is shown once, and senders are allowed on its page", async ({
  page,
}) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/data/sources"));

  await page.getByRole("button", { name: "Add source" }).click();
  const form = page.getByRole("form", { name: "Add source" });
  await form.getByLabel("Kind").selectOption({ label: "Email" });
  await form.getByLabel("Name").fill("Forwarded mail");
  await form.getByLabel("Allowed senders").fill("Alice@example.com\n@example.org");
  await form.getByRole("button", { name: "Add" }).click();

  const shown = page.getByRole("region", { name: "Shown once" });
  await expect(shown.getByLabel("Address")).toHaveValue("in-address1@in.aios.test");
  await shown.getByRole("button", { name: "Done" }).click();
  const source = api.sources.find((s) => s.name === "Forwarded mail");
  expect(source?.allowed_senders).toEqual(["Alice@example.com", "@example.org"]);

  await page.goto(inOrg(`/data/sources/${source!.id}`));
  await expect(page.getByText("in-address1@in.aios.test")).toBeHidden();
  await page.getByRole("textbox", { name: "Allowed senders" }).fill("bob@example.net");
  await page.getByRole("button", { name: "Save senders" }).click();
  await expect
    .poll(() => api.sources.find((s) => s.id === source!.id)?.allowed_senders)
    .toEqual(["bob@example.net"]);

  await page.getByRole("button", { name: "New address" }).click();
  const rotated = page.getByRole("region", { name: "Shown once" });
  await expect(rotated.getByLabel("Address")).toHaveValue("in-address2@in.aios.test");
});

test("email is shown as unavailable where the deployment has no inbound mail", async ({ page }) => {
  const api = await fakeApi(page);
  const why =
    "Email sources need an inbound mail service, which this deployment doesn't have. They're on the roadmap for deployments like this one.";
  api.sourceKinds = api.sourceKinds.map((k) => (k.kind === "email" ? { ...k, unavailable: why } : k));
  await page.goto(inOrg("/data/sources"));

  await page.getByRole("button", { name: "Add source" }).click();
  const form = page.getByRole("form", { name: "Add source" });
  await expect(form.getByRole("option", { name: "Email (unavailable)" })).toBeDisabled();
  await expect(form).toContainText(`Email: ${why}`);
  await expect(form.getByRole("option", { name: "Webhook" })).toBeEnabled();
});
