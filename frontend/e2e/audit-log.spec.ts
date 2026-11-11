import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * The Organisation page's Audit log tab (admins only): who did what, newest first, filtered by
 * actor, action and date; and the approval rules on its Settings tab, which admins raise.
 */

const SAM = { id: 2, first_name: "Sam", last_name: "Jones" };

function withLog() {
  return async ({ page }: { page: import("@playwright/test").Page }) => {
    const api = await fakeApi(page);
    api.organisation.members.push({ user: SAM, joined_at: "2026-10-15T09:00:00Z", invite_id: null });
    api.addAuditEntry({
      at: "2026-11-01T09:00:00Z",
      action: "agent.archived",
      target_kind: "agent",
      target_id: "4",
      details: { name: "Scout" },
    });
    api.addAuditEntry({
      at: "2026-11-03T09:00:00Z",
      action: "member.role_changed",
      target_kind: "member",
      target_id: "2",
      details: { from: "editor", to: "participant" },
    });
    api.addAuditEntry({
      at: "2026-11-05T09:00:00Z",
      actor: { kind: "agent", user: SAM, agent: { id: 4, name: "Scout" }, run_id: 9 },
      action: "data.written",
      target_kind: "table",
      target_id: "data.fish",
      details: { table: "data.fish", operation: "insert", row_count: 2 },
    });
    return api;
  };
}

test("an admin reads the audit log newest first", async ({ page }) => {
  await withLog()({ page });
  await page.goto(inOrg("/organisation"));
  await page.getByRole("tab", { name: "Audit log" }).click();

  const rows = page.getByRole("list", { name: "Audit log" }).getByRole("listitem");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("data.written");
  await expect(rows.nth(0)).toContainText("Scout (run #9)");
  await expect(rows.nth(0)).toContainText("table data.fish");
  await expect(rows.nth(0)).toContainText("row_count: 2");
  await expect(rows.nth(1)).toContainText("member.role_changed");
  await expect(rows.nth(1)).toContainText("editor → participant");
  await expect(rows.nth(2)).toContainText("agent.archived");
  await expect(rows.nth(2)).toContainText("Terry Tester");
  await expect(rows.nth(2)).toContainText("agent Scout");
});

test("an admin filters the audit log by actor, action and date", async ({ page }) => {
  await withLog()({ page });
  const asked: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/audit-log")) asked.push(new URL(r.url()).search);
  });
  await page.goto(inOrg("/organisation/audit-log"));
  const rows = page.getByRole("list", { name: "Audit log" }).getByRole("listitem");
  await expect(rows).toHaveCount(3);

  await page.getByRole("combobox", { name: "Actor" }).selectOption({ label: "Terry Tester" });
  await expect(rows).toHaveCount(2);
  await page.getByRole("combobox", { name: "Action" }).fill("member");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("member.role_changed");

  await page.getByRole("combobox", { name: "Actor" }).selectOption({ label: "Anyone" });
  await page.getByRole("combobox", { name: "Action" }).fill("");
  await page.getByLabel("From", { exact: true }).fill("2026-11-02");
  await page.getByLabel("To", { exact: true }).fill("2026-11-04");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("member.role_changed");
  expect(asked.at(-1)).toBe("?since=2026-11-02&until=2026-11-04");
  expect(asked).toContain("?actor=1&action=member");
});

test("an empty log says so", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/organisation/audit-log"));

  await expect(page.getByText("Nothing matches")).toBeVisible();
});

for (const role of ["editor", "participant"] as const) {
  test(`an ${role} has no Audit log tab`, async ({ page }) => {
    const api = await withLog()({ page });
    api.actAs(role);
    await page.goto(inOrg("/organisation"));

    await expect(page.getByRole("tab", { name: "Members" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Audit log" })).toBeHidden();
    // Opened by its link, the page falls back to its first tab.
    await page.goto(inOrg("/organisation/audit-log"));
    await expect(page.getByRole("list", { name: "Members" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Audit log" })).toBeHidden();
  });
}

test("an admin raises an approval rule, but can't lower one below its default", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));

  const rules = page.getByRole("region", { name: "Approval rules" });
  const addColumn = rules.getByRole("combobox", { name: "Add a column" });
  await expect(addColumn).toHaveValue("editor");
  await addColumn.selectOption("admin");
  await expect(addColumn).toHaveValue("admin");
  expect(api.writes).toContainEqual(["PUT", orgApi("/approval-rules/add_column"), { role: "admin" }]);

  // A rule whose default is an admin offers nothing lower.
  const drops = rules.getByRole("combobox", { name: "Drop a column" });
  await expect(drops).toHaveValue("admin");
  await expect(drops.getByRole("option")).toHaveText(["Admin"]);
});

test("an editor sees the approval rules but can't change them", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  await page.goto(inOrg("/organisation/settings"));

  const rules = page.getByRole("region", { name: "Approval rules" });
  await expect(rules.getByRole("combobox")).toHaveCount(0);
  await expect(rules).toContainText("Drop a column");
  await expect(rules).toContainText("Admins");
});
