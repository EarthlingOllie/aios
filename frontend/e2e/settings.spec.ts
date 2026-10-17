import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Settings page: the run history export's switch, when it last ran, and deleting it. */

test("switching the export on sets the store up, then says when it last ran", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/settings");

  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByText("Coming soon.")).toBeHidden();
  const toggle = page.getByRole("checkbox", { name: "Export my run history to my data store" });
  const status = page.getByRole("status", { name: "Run history export" });
  const remove = page.getByRole("button", { name: "Delete exported history" });
  await expect(toggle).not.toBeChecked();
  await expect(status).toHaveText("Off. Nothing has been exported.");
  await expect(remove).toBeDisabled();

  await toggle.check();
  await expect(toggle).toBeChecked();
  expect(api.writes).toEqual([["PUT", "/api/settings", { export_run_history: true }]]);
  await expect(status).toHaveText("Setting up the history tables in your data store…");

  // The workers set the store up and export: the page keeps asking until they have.
  api.settings.history_ready = true;
  await expect(status).toHaveText("Waiting for the first export…");
  api.settings.history_exported_at = "2026-10-16T10:00:00Z";
  await expect(status).toContainText("Last exported");
  await expect(remove).toBeEnabled();

  // The setting persists.
  await page.reload();
  await expect(toggle).toBeChecked();
  await expect(status).toContainText("Last exported");
});

test("with no runs to export, the page settles once the store is set up", async ({ page }) => {
  const api = await fakeApi(page);
  let asked = 0;
  page.on("request", (r) => {
    if (r.method() === "GET" && new URL(r.url()).pathname === "/api/settings") asked++;
  });
  await page.goto("/settings");
  await page.getByRole("checkbox", { name: "Export my run history to my data store" }).check();
  const status = page.getByRole("status", { name: "Run history export" });
  await expect(status).toHaveText("Setting up the history tables in your data store…");

  // The export, with nothing pending, still records that it caught up.
  api.settings.history_ready = true;
  api.settings.history_exported_at = "2026-10-16T10:00:00Z";
  await expect(status).toContainText("Last exported");

  // And the page stops asking.
  const settled = asked;
  await page.waitForTimeout(2500);
  expect(asked).toBe(settled);
});

test("switching it off keeps what was exported, which can then be deleted", async ({ page }) => {
  const api = await fakeApi(page);
  api.settings = {
    export_run_history: true,
    history_ready: true,
    history_exported_at: "2026-10-16T10:00:00Z",
  };
  await page.goto("/settings");

  const toggle = page.getByRole("checkbox", { name: "Export my run history to my data store" });
  await toggle.uncheck();
  await expect(toggle).not.toBeChecked();
  await expect(page.getByRole("status", { name: "Run history export" })).toContainText(
    "Off. What was exported is kept.",
  );

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete exported history" }).click();
  await expect(page.getByText("Exported history deleted.")).toBeVisible();
  expect(api.exportedHistoryDeletes).toBe(1);
});

test("deleting exported history asks first", async ({ page }) => {
  const api = await fakeApi(page);
  api.settings = { export_run_history: false, history_ready: true, history_exported_at: null };
  await page.goto("/settings");

  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Delete exported history" }).click();
  await expect(page.getByText("Exported history deleted.")).toBeHidden();
  expect(api.exportedHistoryDeletes).toBe(0);
});

test("the Settings link opens the page", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Settings" }).first().click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "Run history export" })).toBeVisible();
});
