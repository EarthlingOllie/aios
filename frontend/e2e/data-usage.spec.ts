import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Data page's storage caps: usage on the Overview, and uploads refused at the file cap. */

const overview = (page: Page) => page.getByRole("region", { name: "Data store" });
const meter = (page: Page, name: string) => overview(page).getByRole("meter", { name });
const GB = 1024 ** 3;
const MB = 1024 ** 2;
const file = (name: string) => ({
  name,
  mimeType: "text/markdown",
  buffer: Buffer.from(`# ${name}`),
});

test("the Overview shows usage against both caps", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreUsage = { ...api.dataStoreUsage, db_bytes: 12 * MB, files_bytes: 300 * MB };
  await page.goto("/data");

  await expect(meter(page, "Data stored")).toHaveAttribute("aria-valuetext", "12 MB of 1.0 GB");
  await expect(meter(page, "Documents")).toHaveAttribute("aria-valuetext", "300 MB of 5.0 GB");
  await expect(overview(page)).toContainText("12 MB of 1.0 GB");
  await expect(overview(page).getByRole("alert")).toHaveCount(0);
});

test("the Overview warns as usage nears a cap, and says what's refused at one", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.dataStoreUsage = { ...api.dataStoreUsage, db_bytes: 0.9 * GB, files_bytes: 5 * GB };
  await page.goto("/data");

  await expect(overview(page)).toContainText("Your data is nearly at its cap.");
  await expect(overview(page).getByRole("alert")).toContainText(
    "Your documents are at their cap: uploads are refused, and your agents can't add or change rows. Delete some to make room.",
  );
});

test("the Overview says usage isn't measured yet for a new store", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreUsage = {
    ...api.dataStoreUsage,
    db_bytes: null,
    files_bytes: null,
    measured_at: null,
  };
  await page.goto("/data");

  await expect(meter(page, "Data stored")).toHaveAttribute("aria-valuetext", "Not measured yet");
});

test("the Overview reads usage again while it's open", async ({ page }) => {
  const api = await fakeApi(page);
  await page.clock.install({ time: new Date("2026-10-17T09:00:00Z") });
  await page.goto("/data");
  await expect(meter(page, "Documents")).toHaveAttribute("aria-valuetext", "0 B of 5.0 GB");

  // The schedule measures the store again.
  api.dataStoreUsage = { ...api.dataStoreUsage, files_bytes: 2 * GB };
  await page.clock.runFor(60_000);

  await expect(meter(page, "Documents")).toHaveAttribute("aria-valuetext", "2.0 GB of 5.0 GB");
});

test("a store at its file cap can't take uploads, here or in the chat", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreUsage = { ...api.dataStoreUsage, files_bytes: 5 * GB };
  await page.goto("/data?tab=documents");

  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
  await expect(page.getByRole("alert", { name: "Storage" })).toContainText(
    "Your documents are at their cap: uploads are refused",
  );

  await page.goto("/");
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
  expect(api.writes).toEqual([]);
});

test("a store at its database cap can't take uploads either", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreUsage = { ...api.dataStoreUsage, db_bytes: GB };
  await page.goto("/data?tab=documents");

  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
  await expect(page.getByRole("alert", { name: "Storage" })).toContainText(
    "Your data is at its cap: your agents can't add or change rows, and documents can't be uploaded.",
  );
});

test("deleting documents at the file cap lets uploads in again", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("big.pdf");
  api.dataStoreUsage = { ...api.dataStoreUsage, files_bytes: 5 * GB };
  await page.goto("/data?tab=documents");
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete big.pdf" }).click();

  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  await expect(page.getByRole("alert", { name: "Storage" })).toBeHidden();
});

test("uploads refused for the cap say why, each of them, and uploads stop", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/data?tab=documents");
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  // Filled (from the chat, say) since the page last asked.
  api.dataStoreUsage = { ...api.dataStoreUsage, files_bytes: 5 * GB };

  await page
    .locator('input[type="file"]')
    .first()
    .setInputFiles([file("big.md"), file("more.md")]);

  const errors = page.getByRole("alert").filter({ hasText: "big.md" });
  await expect(errors).toContainText(
    "big.md: Your data store is full: its documents use 5.0 GB of the 5.0 GB allowed. Delete some to make room.",
  );
  await expect(errors).toContainText("more.md: Your data store is full");
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
});
