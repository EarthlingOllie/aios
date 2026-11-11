import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Sources on the Data page: the read-only Sources tab listing Uploads for every member, with no
 * way to pause or remove it, and each document's source on the Documents tab, filterable by
 * source.
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
    await expect(uploads).toContainText("Active");
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
