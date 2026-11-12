import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Reversible drops: a request that drops a table or column shows what it would remove and
 * takes the tables' names typed to approve; the Data page's Dropped section lists what's
 * dropped with when it's purged, and restores it.
 */

const SUMMARY = "Drop column weight_kg from data.fish; drop table data.eels";

function dropRequest(api: FakeApi) {
  return api.addSchemaChange({
    summary: SUMMARY,
    sql: 'ALTER TABLE "data"."fish" RENAME COLUMN "weight_kg" TO "_dropped_20261014090000_weight_kg";',
    confirm_with: "fish, eels",
    impact: [
      { table: "fish", column: "weight_kg", rows: 3, values: 2, columns: null, estimated: false },
      {
        table: "eels",
        column: null,
        rows: 12,
        values: null,
        columns: ["id", "length"],
        estimated: false,
      },
    ],
  });
}

const card = (page: Page) => page.getByRole("region", { name: `Schema change: ${SUMMARY}` });

test("a drop's card shows what it removes, and approves only with the names typed", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const request = dropRequest(api);

  await page.goto(inOrg("/data/approvals"));

  const removes = card(page).getByRole("list", { name: "What it removes" });
  await expect(removes).toContainText("Column weight_kg of data.fish: a value in 2 of 3 rows");
  await expect(removes).toContainText("Table data.eels: 12 rows, with columns id, length");
  const approve = card(page).getByRole("button", { name: "Approve" });
  await expect(approve).toBeDisabled();
  const typed = card(page).getByLabel("Type fish, eels to confirm");
  await typed.fill("fish");
  await expect(approve).toBeDisabled();
  await typed.fill("fish, eels");
  await approve.click();

  await expect(card(page)).toContainText("Applying");
  expect(api.writes).toContainEqual([
    "POST",
    orgApi(`/approvals/${request.id}/approve`),
    { confirm: "fish, eels" },
  ]);
});

test("a refusal from the server is shown on the card", async ({ page }) => {
  const api = await fakeApi(page);
  const request = dropRequest(api);
  // The names it takes changed (the page is out of date): the server refuses the old ones.
  await page.goto(inOrg("/data/approvals"));
  await expect(card(page).getByLabel("Type fish, eels to confirm")).toBeVisible();
  request.confirm_with = "fish";

  await card(page).getByLabel("Type fish, eels to confirm").fill("fish, eels");
  await card(page).getByRole("button", { name: "Approve" }).click();

  await expect(card(page).getByRole("alert")).toContainText("Type `fish` to confirm");
  await expect(card(page)).toContainText("Waiting for you");
});

test("the Dropped section lists what's dropped, with when it's purged", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDropped({ kind: "table", table: "eels", purge_in_days: 29.5 });
  api.addDropped({
    kind: "column",
    table: "fish",
    column: "name",
    blocked:
      "`data.fish` already has a column `name` again: one was added after this one was dropped, so this one can't take its name back.",
  });

  await page.goto(inOrg("/data/dropped"));

  const eels = page.getByRole("listitem", { name: "Table data.eels" });
  await expect(eels).toContainText("Purged in 29 days");
  const name = page.getByRole("listitem", { name: "Column name of data.fish" });
  await expect(name).toContainText("already has a column `name` again");
  await expect(name.getByRole("button", { name: "Restore" })).toBeDisabled();
});

test("restoring takes it off the list once it's back", async ({ page }) => {
  const api = await fakeApi(page);
  const eels = api.addDropped({ kind: "table", table: "eels" });

  await page.goto(inOrg("/data/dropped"));
  await page
    .getByRole("listitem", { name: "Table data.eels" })
    .getByRole("button", { name: "Restore" })
    .click();

  await expect(page.getByRole("listitem", { name: "Table data.eels" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Dropped" })).toContainText("Nothing is dropped");
  expect(api.writes).toContainEqual(["POST", orgApi(`/dropped/${eels.id}/restore`), null]);
});

test("a restore refused once it ran says why", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDropped({ kind: "table", table: "eels", refuseRestore: "`data.eels` exists again." });

  await page.goto(inOrg("/data/dropped"));
  await page
    .getByRole("listitem", { name: "Table data.eels" })
    .getByRole("button", { name: "Restore" })
    .click();

  await expect(page.getByRole("listitem", { name: "Table data.eels" })).toContainText(
    "Couldn't restore it: `data.eels` exists again.",
  );
});

test("a restore that never comes stops being waited for, and can be tried again", async ({
  page,
}) => {
  const api = await fakeApi(page);
  // The worker gives up without a word (it couldn't reach the store, say).
  api.addDropped({ kind: "table", table: "eels", stallRestore: true });
  await page.clock.install();

  await page.goto(inOrg("/data/dropped"));
  const eels = page.getByRole("listitem", { name: "Table data.eels" });
  await eels.getByRole("button", { name: "Restore" }).click();
  await expect(eels.getByRole("button", { name: "Restore" })).toBeDisabled();
  await page.clock.runFor("01:00");

  await expect(eels).toContainText("It hasn't been restored yet");
  await expect(eels.getByRole("button", { name: "Restore" })).toBeEnabled();
});
