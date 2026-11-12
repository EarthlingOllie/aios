import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Budgets: the organisation's monthly budget and role allowances on the Settings tab (admins set
 * them; everyone else reads them), and each member's allowance and spend this month on the
 * Members tab, which admins override.
 */

const SAM = { id: 2, first_name: "Sam", last_name: "Jones" };

test("an admin sets the budget and role allowances, and sees this month's spend against it", async ({ page }) => {
  const api = await fakeApi(page);
  api.budget.spent = 42.5;
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Budget" });
  await expect(section.getByRole("definition").first()).toHaveText("£42.50 spent, no budget set");

  await section.getByRole("spinbutton", { name: "Monthly budget (£)" }).fill("50");
  await section.getByRole("spinbutton", { name: "Participant allowance (£)" }).fill("5");
  await section.getByRole("button", { name: "Update budget" }).click();
  await expect(section.getByRole("status")).toHaveText("Saved");
  expect(api.writes).toEqual([
    ["PUT", orgApi("/organisation/budget"), { budget: 50, role_allowances: { admin: null, editor: null, participant: 5 } }],
  ]);
  // 85%: past the warning threshold.
  await expect(section.getByRole("definition").first()).toHaveText("£42.50 of £50.00 (85%)");
  await expect(section.getByRole("note")).toHaveText("Spending is past the warning threshold.");

  await page.reload();
  await expect(section.getByRole("spinbutton", { name: "Monthly budget (£)" })).toHaveValue("50");
  await section.getByRole("spinbutton", { name: "Monthly budget (£)" }).fill("-3");
  await expect(section.getByRole("button", { name: "Update budget" })).toBeDisabled();
});

test("at the budget, everyone is told new work is refused", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  api.budget.budget = 10;
  api.budget.spent = 10;
  api.budget.role_allowances.participant = 2;
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Budget" });
  await expect(section.getByRole("note")).toContainText("The budget has been reached");
  await expect(section.getByRole("spinbutton")).toHaveCount(0);
  await expect(section.getByRole("definition")).toHaveText(["£10.00 of £10.00 (100%)", "None", "None", "£2.00"]);
});

test("the Members tab shows each member's allowance and spend, and an admin overrides one", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: SAM, joined_at: "2026-10-15T09:00:00Z", invite_id: null, role: "participant" });
  api.budget.role_allowances.participant = 5;
  api.budget.spends = { [api.me.id]: 12, [SAM.id]: 4.25 };
  await page.goto(inOrg("/organisation"));
  const members = page.getByRole("list", { name: "Members" }).getByRole("listitem");
  await expect(members.nth(0)).toContainText("£12.00 this month, no allowance");
  await expect(members.nth(1)).toContainText("£4.25 this month of £5.00 allowance");

  const own = page.getByRole("spinbutton", { name: "Sam Jones's own allowance (£)" });
  await own.fill("20");
  await page.getByRole("form", { name: "Sam Jones's allowance" }).getByRole("button", { name: "Set" }).click();
  await expect(members.nth(1)).toContainText("£4.25 this month of £20.00 own allowance");
  expect(api.writes).toEqual([["PUT", orgApi(`/organisation/members/${SAM.id}/allowance`), { allowance: 20 }]]);
});

test("a member who isn't an admin sees only their own spend, and can't override it", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: SAM, joined_at: "2026-10-15T09:00:00Z", invite_id: null, role: "admin" });
  api.actAs("editor");
  api.budget.spends = { [api.me.id]: 3, [SAM.id]: 9 };
  await page.goto(inOrg("/organisation"));
  const members = page.getByRole("list", { name: "Members" }).getByRole("listitem");
  await expect(members.nth(0)).toContainText("£3.00 this month, no allowance");
  await expect(members.nth(1)).not.toContainText("this month");
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
});
