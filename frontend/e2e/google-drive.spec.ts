import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Google Drive on the Data page: unavailable, with the reason, until Google has verified the
 * deployment (and Connect isn't offered); a connected Drive shows its health; and a sync's
 * skipped files (unsupported, or too big) are listed on its source's page.
 */

const UNVERIFIED =
  "Google Drive is unavailable until Google has verified this deployment's access to Drive: connecting would end at Google's consent error.";

const providerRow = (page: Page, title: string) =>
  page.getByRole("list", { name: "Connectors" }).getByRole("listitem").filter({ hasText: title });

test("an unverified deployment lists Drive as unavailable, with why, and doesn't offer connecting", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.connectorProviders = api.connectorProviders.map((p) =>
    p.provider === "google_drive" ? { ...p, unavailable: UNVERIFIED } : p,
  );
  await page.goto(inOrg("/data/sources"));

  const row = providerRow(page, "Google Drive");
  await expect(row).toContainText("until Google has verified");
  const connect = row.getByRole("button", { name: "Connect" });
  await expect(connect).toBeDisabled();
  await expect(connect).toHaveAttribute("title", UNVERIFIED);
});

test("a connected Drive shows its health on the Data page", async ({ page }) => {
  const api = await fakeApi(page);
  const source = api.addSource("Google Drive", "connector", {
    provider: "google_drive",
    status: "failing",
    health: "failing",
  });
  api.addConnector("google_drive", {
    scope: [{ id: "1PoliciesFolder", name: "Policies", restricted: false }],
    source_id: source.id,
  });
  await page.goto(inOrg("/data/sources"));

  const row = providerRow(page, "Google Drive");
  await expect(row).toContainText("Syncs Policies");
  await expect(row.getByLabel("Health: Failing")).toBeVisible();
  await expect(
    page.getByRole("list", { name: "Sources" }).getByRole("listitem").filter({ hasText: "Google Drive" }),
  ).toContainText("Failing");
});

test("a sync's skipped files are listed on its source's page, each with why", async ({ page }) => {
  const api = await fakeApi(page);
  const source = api.addSource("Google Drive", "connector", {
    provider: "google_drive",
    syncs: [
      api.sync({
        kind: "full",
        added: 6,
        dropped: 2,
        skipped_files: [
          { name: "logo.png", reason: "Unsupported file type (image/png)." },
          { name: "Handbook scans.pdf", reason: "Larger than the 50 MiB file cap." },
        ],
      }),
    ],
  });
  await page.goto(inOrg(`/data/sources/${source.id}`));

  const syncs = page.getByRole("list", { name: "Syncs" });
  await expect(syncs).toContainText("6 added · 2 dropped");
  await syncs.getByText("2 files skipped").click();
  const skipped = page.getByRole("list", { name: "Skipped files" });
  await expect(skipped.getByRole("listitem")).toHaveText([
    "logo.png: Unsupported file type (image/png).",
    "Handbook scans.pdf: Larger than the 50 MiB file cap.",
  ]);
});
