import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Who may write each data table, on the Data page's Tables: its owner ("No owner" when it has
 * none, read-only while its owner is archived) and the agents it's shared with. Editors and admins
 * assign, transfer and clear owners, and share and unshare; participants only see it. Archiving an
 * owner lists the tables it leaves read-only.
 */

const card = (page: Page, name: string) =>
  page
    .getByRole("list", { name: "Tables" })
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { name }) });
const access = (page: Page, name: string) => page.getByRole("region", { name: `Access to ${name}` });

test("each table shows its owner, its shares, or that it has none", async ({ page }) => {
  const api = await fakeApi(page);
  const scribe = api.addAgent("Scribe");
  const helper = api.addAgent("Helper");
  const gone = api.addAgent("Gone");
  gone.archived_at = "2026-11-01T00:00:00Z";
  api.addTable("notes", ["note"], [], { owner: scribe.id, shares: [helper.id] });
  api.addTable("fish", ["name"]);
  api.addTable("old", ["title"], [], { owner: gone.id });
  await page.goto(inOrg("/data/tables"));

  await expect(access(page, "notes")).toContainText("Owned by Scribe");
  await expect(access(page, "notes").getByRole("list", { name: "Shared with" })).toHaveText(["Helper"]);
  await expect(access(page, "fish")).toContainText("No owner");
  await expect(access(page, "old")).toContainText("Read-only: its owner, Gone, is archived");
});

test("an editor assigns an owner, shares, unshares and transfers", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  const scribe = api.addAgent("Scribe");
  const helper = api.addAgent("Helper");
  api.addTable("fish", ["name"]);
  await page.goto(inOrg("/data/tables"));
  const fish = access(page, "fish");

  await fish.getByLabel("Assign an owner").selectOption("Scribe");
  await fish.getByRole("button", { name: "Assign" }).click();
  await expect(fish).toContainText("Owned by Scribe");

  // The owner isn't offered to share with.
  const share = fish.getByLabel("Share with");
  await expect(share.getByRole("option")).toHaveText(["Choose an agent", "Helper"]);
  await share.selectOption("Helper");
  await fish.getByRole("button", { name: "Share", exact: true }).click();
  await expect(fish.getByRole("list", { name: "Shared with" })).toHaveText(["Helper"]);

  await fish.getByRole("button", { name: "Unshare with Helper" }).click();
  await expect(fish.getByRole("list", { name: "Shared with" }).getByRole("listitem")).toHaveCount(0);

  // Transferring needs an admin by the approval rules: refused, and it says why.
  await fish.getByLabel("Transfer to").selectOption("Helper");
  await fish.getByRole("button", { name: "Transfer" }).click();
  await expect(fish.getByRole("alert")).toContainText("needs an admin");
  await expect(fish).toContainText("Owned by Scribe");

  expect(api.writes.filter(([, path]) => path.includes("/data/tables/"))).toEqual([
    ["PUT", orgApi("/data/tables/fish/owner"), { agent_id: scribe.id }],
    ["POST", orgApi("/data/tables/fish/shares"), { agent_id: helper.id }],
    ["DELETE", orgApi(`/data/tables/fish/shares/${helper.id}`), null],
    ["PUT", orgApi("/data/tables/fish/owner"), { agent_id: helper.id }],
  ]);
});

test("an admin transfers a table and clears its owner", async ({ page }) => {
  const api = await fakeApi(page);
  const scribe = api.addAgent("Scribe");
  api.addAgent("Heir");
  api.addTable("notes", ["note"], [], { owner: scribe.id });
  await page.goto(inOrg("/data/tables"));
  const notes = access(page, "notes");

  await notes.getByLabel("Transfer to").selectOption("Heir");
  await notes.getByRole("button", { name: "Transfer" }).click();
  await expect(notes).toContainText("Owned by Heir");

  await notes.getByRole("button", { name: "Clear owner" }).click();
  await expect(notes).toContainText("No owner");
  await expect(notes.getByLabel("Share with")).toHaveCount(0);
});

test("participants see who may write a table but can't change it", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  const scribe = api.addAgent("Scribe");
  const helper = api.addAgent("Helper");
  api.addTable("notes", ["note"], [], { owner: scribe.id, shares: [helper.id] });
  await page.goto(inOrg("/data/tables"));

  await expect(access(page, "notes")).toContainText("Owned by Scribe");
  await expect(card(page, "notes").getByRole("button")).toHaveCount(0);
  await expect(card(page, "notes").getByRole("combobox")).toHaveCount(0);
});

test("archiving an owner lists the tables it leaves read-only", async ({ page }) => {
  const api = await fakeApi(page);
  const scribe = api.addAgent("Scribe");
  api.addTable("notes", ["note"], [], { owner: scribe.id });
  api.addTable("fish", ["name"]);
  await page.goto(inOrg(`/agents/${scribe.id}`));

  await page.getByRole("button", { name: "Archive", exact: true }).click();
  const usedBy = page.getByRole("dialog", { name: /Archive/ }).getByLabel("Used by");
  await expect(usedBy).toContainText(/Data tables left read-only\s*notes/);
  await page.getByRole("dialog").getByRole("button", { name: "Archive agent" }).click();

  await page.goto(inOrg("/data/tables"));
  await expect(access(page, "notes")).toContainText("Read-only: its owner, Scribe, is archived");
});
