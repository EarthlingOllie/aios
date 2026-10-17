import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi } from "./fake-api";

/** The Data page's Tables: the store's own tables, and their rows. */

const tableList = (page: Page) => page.getByRole("list", { name: "Tables" });
const tableCard = (page: Page, name: string) =>
  tableList(page).getByRole("listitem").filter({ has: page.getByRole("heading", { name }) });
const grid = (page: Page, name: string) => page.getByRole("table", { name: `Rows of ${name}` });
/** The grid's rows (its header first), as their cells' text. */
const gridRows = (page: Page, name: string) => grid(page, name).getByRole("row");
/** The text of the grid's first column, top to bottom. */
const firstColumn = (page: Page, name: string) =>
  grid(page, name).locator("tbody tr td:first-child");

function addFish(api: FakeApi) {
  return api.addTable(
    "fish",
    [
      { name: "id", type: "bigint", nullable: false },
      { name: "name", type: "text", nullable: false },
      { name: "weight_kg", type: "numeric", description: "As weighed on the boat" },
    ],
    [
      ["1", "pike", "4.2"],
      ["2", "perch", "0.8"],
      ["3", "carp", "7.5"],
    ],
    { description: "Fish the user has caught" },
  );
}

test("Tables lists each table with its columns and row count", async ({ page }) => {
  const api = await fakeApi(page);
  addFish(api);
  api.addTable("readings", ["v:integer"], [], { estimated: true, rowCount: 1234567 });
  await page.goto("/data");

  await page.getByRole("tab", { name: "Tables" }).click();

  await expect(page).toHaveURL(/\/data\?tab=tables$/);
  const fish = tableCard(page, "fish");
  await expect(fish).toContainText("Fish the user has caught");
  await expect(fish).toContainText("3 rows");
  const columns = fish.getByRole("table", { name: "Columns of fish" });
  await expect(columns.getByRole("row")).toHaveText([
    /Column\s*Type\s*Description/,
    /id\s*bigint/,
    /name\s*text/,
    /weight_kg\s*numeric\s*As weighed on the boat/,
  ]);
  await expect(tableCard(page, "readings")).toContainText("About 1,234,567 rows");
});

test("opening a table shows its rows, and sorting by a column reorders them", async ({ page }) => {
  const api = await fakeApi(page);
  addFish(api);
  await page.goto("/data?tab=tables");

  await tableCard(page, "fish").getByRole("link", { name: "fish" }).click();

  await expect(page).toHaveURL(/\/data\?tab=tables&table=fish$/);
  await expect(page.getByRole("heading", { name: "fish", level: 2 })).toBeVisible();
  await expect(gridRows(page, "fish")).toHaveText([
    /id\s*name\s*weight_kg/,
    /1\s*pike\s*4\.2/,
    /2\s*perch\s*0\.8/,
    /3\s*carp\s*7\.5/,
  ]);

  const weight = grid(page, "fish").getByRole("button", { name: "weight_kg" });
  await weight.click();
  await expect(firstColumn(page, "fish")).toHaveText(["2", "1", "3"]);
  await expect(page.getByRole("columnheader", { name: "weight_kg" })).toHaveAttribute(
    "aria-sort",
    "ascending",
  );
  await expect(page).toHaveURL(/sort=weight_kg/);

  await weight.click();
  await expect(firstColumn(page, "fish")).toHaveText(["3", "1", "2"]);
  await expect(page.getByRole("columnheader", { name: "weight_kg" })).toHaveAttribute(
    "aria-sort",
    "descending",
  );

  // A third click goes back to the order they're stored in.
  await weight.click();
  await expect(firstColumn(page, "fish")).toHaveText(["1", "2", "3"]);
  await expect(page).not.toHaveURL(/sort=/);

  // And a link back to the list.
  await page.getByRole("link", { name: "All tables" }).click();
  await expect(page).toHaveURL(/\/data\?tab=tables$/);
  await expect(tableCard(page, "fish")).toBeVisible();
});

test("a column added since the list was read heads its own values", async ({ page }) => {
  const api = await fakeApi(page);
  const fish = addFish(api);
  await page.goto("/data?tab=tables");
  await expect(tableCard(page, "fish")).toBeVisible();

  // An agent adds a column before the table is opened.
  fish.columns.splice(1, 0, {
    name: "colour",
    type: "text",
    nullable: true,
    description: null,
    embedding: false,
  });
  fish.rows = fish.rows.map(([id, ...rest]) => [id, "green", ...rest]);
  await tableCard(page, "fish").getByRole("link", { name: "fish" }).click();

  await expect(gridRows(page, "fish").first()).toHaveText(/id\s*colour\s*name\s*weight_kg/);
  await expect(gridRows(page, "fish").nth(1)).toHaveText(/1\s*green\s*pike\s*4\.2/);
});

test("a big table's rows come a page at a time", async ({ page }) => {
  const api = await fakeApi(page);
  api.addTable(
    "readings",
    ["n:integer"],
    Array.from({ length: 120 }, (_, i) => [String(i + 1)]),
  );
  await page.goto("/data?tab=tables&table=readings");

  await expect(firstColumn(page, "readings")).toHaveCount(50);
  await expect(page.getByText("Rows 1–50")).toBeVisible();
  await expect(page.getByRole("button", { name: "Previous" })).toBeDisabled();

  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("Rows 51–100")).toBeVisible();
  await expect(firstColumn(page, "readings").first()).toHaveText("51");
  await expect(page).toHaveURL(/offset=50/);

  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("Rows 101–120")).toBeVisible();
  await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();

  // Sorting starts again from the first row.
  await grid(page, "readings").getByRole("button", { name: "n" }).click();
  await grid(page, "readings").getByRole("button", { name: "n" }).click();
  await expect(page.getByText("Rows 1–50")).toBeVisible();
  await expect(firstColumn(page, "readings").first()).toHaveText("120");
  expect(api.rowReads.every((q) => /limit=50/.test(q))).toBe(true);
});

test("embeddings show as their dimension, jsonb as it's laid out, and NULL as NULL", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addTable(
    "notes",
    ["id:integer", "meta:jsonb", "vec:vector(1024)"],
    [
      ["1", '{\n    "a": "x"\n}', "vector(1024)"],
      ["2", null, null],
    ],
  );
  await page.goto("/data?tab=tables&table=notes");

  const first = gridRows(page, "notes").nth(1);
  await expect(first.getByRole("cell").nth(2)).toHaveText("vector(1024)");
  // The layout's line breaks are kept.
  expect(await first.getByRole("cell").nth(1).innerText()).toBe('{\n    "a": "x"\n}');
  await expect(gridRows(page, "notes").nth(2).getByRole("cell").nth(1)).toHaveText("NULL");
  // An embedding has nothing to sort by.
  await expect(grid(page, "notes").getByRole("button", { name: "vec" })).toHaveCount(0);
  await expect(grid(page, "notes").getByRole("button", { name: "meta" })).toBeVisible();
});

test("a wide value is cut short, and shown in full on asking", async ({ page }) => {
  const api = await fakeApi(page);
  const body = "word ".repeat(400) + "the end";
  api.addTable("notes", ["id:integer", "body"], [["1", body]]);
  await page.goto("/data?tab=tables&table=notes");

  const cell = gridRows(page, "notes").nth(1).getByRole("cell").nth(1);
  await expect(cell).toContainText("…");
  await expect(cell).not.toContainText("the end");

  await cell.getByRole("button", { name: "Show all of body in row 1" }).click();

  const full = page.getByRole("region", { name: "Full value" });
  await expect(full).toContainText("the end");
  await expect(full).toContainText("body, row 1");
  await full.getByRole("button", { name: "Close body in row 1" }).click();
  await expect(full).toBeHidden();
});

test("an empty store says it has no tables yet", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/data?tab=tables");

  await expect(page.getByText("No tables yet.")).toBeVisible();
  await expect(tableList(page)).toBeHidden();
});

test("an empty table says it has no rows", async ({ page }) => {
  const api = await fakeApi(page);
  api.addTable("empty", ["id:integer"]);
  await page.goto("/data?tab=tables&table=empty");

  await expect(page.getByText("This table has no rows yet.")).toBeVisible();
});

test("a store being set up shows its tables once it's ready", async ({ page }) => {
  const api = await fakeApi(page);
  api.dataStoreStatus = "provisioning";
  addFish(api);
  await page.goto("/data?tab=tables");

  await expect(page.getByRole("status", { name: "Data store" })).toContainText(
    "Setting up your data store.",
  );
  await expect(tableList(page)).toBeHidden();

  api.dataStoreStatus = "ready";
  await expect(tableCard(page, "fish")).toBeVisible({ timeout: 10_000 });
});

test("a table that isn't there says so", async ({ page }) => {
  const api = await fakeApi(page);
  addFish(api);
  await page.goto("/data?tab=tables&table=whales");

  await expect(page.getByRole("alert", { name: "Table" })).toContainText(
    "There's no table called “whales”.",
  );
  await page.getByRole("link", { name: "All tables" }).click();
  await expect(tableCard(page, "fish")).toBeVisible();
});

test("tables that couldn't be read say so, and can be tried again", async ({ page }) => {
  const api = await fakeApi(page);
  addFish(api);
  api.tablesFail = true;
  await page.goto("/data?tab=tables");

  const notice = page.getByRole("alert", { name: "Tables" });
  await expect(notice).toContainText("Couldn't load your tables");

  api.tablesFail = false;
  await notice.getByRole("button", { name: "Try again" }).click();
  await expect(tableCard(page, "fish")).toBeVisible();
  await expect(notice).toBeHidden();
});
