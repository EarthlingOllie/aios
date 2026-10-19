import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The Organisation page's run history export: its switch, when it last ran, and deleting it. */

test("switching the export on sets the store up, then says when it last ran", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/organisation");

  await expect(page.getByRole("heading", { name: "Run history export" })).toBeVisible();
  const toggle = page.getByRole("checkbox", { name: "Export the organisation's run history to its data store" });
  const status = page.getByRole("status", { name: "Run history export" });
  const remove = page.getByRole("button", { name: "Delete exported history" });
  await expect(toggle).not.toBeChecked();
  await expect(status).toHaveText("Off. Nothing has been exported.");
  await expect(remove).toBeDisabled();

  await toggle.check();
  await expect(toggle).toBeChecked();
  expect(api.writes).toEqual([["PUT", "/api/organisation/settings", { export_run_history: true }]]);
  await expect(status).toHaveText("Setting up the history tables in the data store…");

  // The workers set the store up and export: the page keeps asking until they have.
  api.settings.history_ready = true;
  await expect(status).toHaveText("Exporting the organisation's runs…");
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
    if (r.method() === "GET" && new URL(r.url()).pathname === "/api/organisation/settings") asked++;
  });
  await page.goto("/organisation");
  await page.getByRole("checkbox", { name: "Export the organisation's run history to its data store" }).check();
  const status = page.getByRole("status", { name: "Run history export" });
  await expect(status).toHaveText("Setting up the history tables in the data store…");

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
    ...api.settings,
    export_run_history: true,
    history_ready: true,
    history_exported_at: "2026-10-16T10:00:00Z",
  };
  await page.goto("/organisation");

  const toggle = page.getByRole("checkbox", { name: "Export the organisation's run history to its data store" });
  await toggle.uncheck();
  await expect(toggle).not.toBeChecked();
  await expect(page.getByRole("status", { name: "Run history export" })).toContainText(
    "Off. What was exported is kept.",
  );

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete exported history" }).click();
  await expect(page.getByText("Exported history deleted.")).toBeVisible();
  expect(api.exportedHistoryDeletes).toBe(1);
  await expect(page.getByRole("status", { name: "Run history export" })).toHaveText(
    "Off. The data store has no exported history.",
  );
});

test("deleting exported history while it's on starts the export over", async ({ page }) => {
  const api = await fakeApi(page);
  api.settings = {
    ...api.settings,
    export_run_history: true,
    history_ready: true,
    history_exported_at: "2026-10-16T10:00:00Z",
  };
  await page.goto("/organisation");
  const status = page.getByRole("status", { name: "Run history export" });
  await expect(status).toContainText("Last exported");

  let asked = "";
  page.once("dialog", (dialog) => {
    asked = dialog.message();
    return dialog.accept();
  });
  await page.getByRole("button", { name: "Delete exported history" }).click();
  await expect(
    page.getByText("Exported history deleted. All the organisation's runs are exported again."),
  ).toBeVisible();
  expect(asked).toContain("switch the export off first");
  expect(api.exportedHistoryDeletes).toBe(1);

  // No longer "Last exported", until the export has copied the runs again.
  await expect(status).toHaveText("Exporting the organisation's runs…");
  api.settings.history_exported_at = "2026-10-16T11:00:00Z";
  await expect(status).toContainText("Last exported");
});

test("deleting exported history asks first", async ({ page }) => {
  const api = await fakeApi(page);
  api.settings = { ...api.settings, history_ready: true };
  await page.goto("/organisation");

  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Delete exported history" }).click();
  await expect(page.getByText("Exported history deleted.")).toBeHidden();
  expect(api.exportedHistoryDeletes).toBe(0);
});

test("embedding is offered only while the export is on, and says when it caught up", async ({
  page,
}) => {
  const api = await fakeApi(page);
  await page.goto("/organisation");
  const exportToggle = page.getByRole("checkbox", {
    name: "Export the organisation's run history to its data store",
  });
  const embedToggle = page.getByRole("checkbox", {
    name: "Embed exported messages so agents can search past runs by meaning",
  });
  const status = page.getByRole("status", { name: "Run history embedding" });
  await expect(exportToggle).not.toBeChecked();
  await expect(embedToggle).toBeHidden();

  await exportToggle.check();
  api.settings.history_ready = true;
  api.settings.history_exported_at = "2026-10-16T10:00:00Z";
  await expect(embedToggle).toBeVisible();
  await expect(embedToggle).not.toBeChecked();
  await expect(status).toHaveText(
    "Off. New messages aren't embedded: agents find them by keyword. Those embedded before are kept, and still found by meaning.",
  );

  await embedToggle.check();
  await expect(embedToggle).toBeChecked();
  expect(api.writes.at(-1)).toEqual(["PUT", "/api/organisation/settings", { embed_run_history: true }]);
  // The backfill: the page keeps asking until it's caught up.
  await expect(status).toHaveText("Embedding the exported messages…");
  api.settings.history_embedded_at = "2026-10-16T11:00:00Z";
  await expect(status).toContainText("Last embedded");

  // Switching the export off switches embedding off, and no longer offers it.
  await exportToggle.uncheck();
  await expect(embedToggle).toBeHidden();
  expect(api.settings.embed_run_history).toBe(false);
});
