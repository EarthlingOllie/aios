import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Limits and usage: the organisation's caps on turns and workflow runs (admins change them, on the
 * Settings tab; everyone else reads them), an agent's and a workflow's own invocable-by and caps
 * in their editors (only admins set one above the organisation's), and the Usage tab's totals,
 * admins' only.
 */

test("an admin overrides a limit, and the others follow the deployment's", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Limits" });
  const calls = section.getByRole("spinbutton", { name: "Model calls per turn" });
  await expect(calls).toHaveValue("");
  await expect(calls).toHaveAttribute("placeholder", "200");

  await calls.fill("50");
  await section.getByRole("button", { name: "Apply" }).click();
  await expect(section.getByRole("status")).toHaveText("Saved");
  expect(api.writes).toEqual([
    [
      "PUT",
      orgApi("/organisation/limits"),
      { turn_model_calls: 50, turn_tokens: null, turn_cost: null, run_model_calls: null, run_cost: null },
    ],
  ]);
  await page.reload();
  await expect(section.getByRole("spinbutton", { name: "Model calls per turn" })).toHaveValue("50");

  await section.getByRole("spinbutton", { name: "Cost per turn (£)" }).fill("0");
  await expect(section.getByRole("button", { name: "Apply" })).toBeDisabled();
});

for (const role of ["editor", "participant"] as const) {
  test(`${role === "editor" ? "an" : "a"} ${role} reads the limits in force`, async ({ page }) => {
    const api = await fakeApi(page);
    api.limits.turn_cost = 5;
    api.actAs(role);
    await page.goto(inOrg("/organisation/settings"));
    const section = page.getByRole("region", { name: "Limits" });
    await expect(section.getByRole("spinbutton")).toHaveCount(0);
    const limits = section.getByRole("definition");
    await expect(limits).toHaveText(["200", "5,000,000", "£5.00", "2,000", "£100.00"]);
    await expect(section).toContainText("Only admins can change the organisation's limits.");
  });
}

test("an editor sets who may mention an agent and lowers its limit, but can't raise one past the organisation's", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  const bot = api.addAgent("Bot");
  await page.goto(inOrg(`/agents/${bot.id}/edit`));
  const fields = page.getByRole("group", { name: "Invocation and limits" });
  await fields.getByRole("combobox", { name: "Who can mention it" }).selectOption("editors");
  const calls = fields.getByRole("spinbutton", { name: "Model calls per turn" });
  await expect(calls).toHaveAttribute("placeholder", "200");

  await calls.fill("500");
  await expect(fields.getByRole("alert")).toContainText("Only admins can set a limit above the organisation's");
  await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();

  await calls.fill("20");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(inOrg(`/agents/${bot.id}`));
  const [, , body] = api.writes.at(-1)!;
  expect(body).toMatchObject({
    invocable_by: "editors",
    limits: { turn_model_calls: 20, turn_tokens: null, turn_cost: null },
  });
  expect(bot.invocable_by).toBe("editors");
  expect(bot.limits).toEqual({ turn_model_calls: 20 });
});

test("an admin may set a workflow's limit above the organisation's", async ({ page }) => {
  const api = await fakeApi(page);
  const flow = api.addWorkflow("Pipeline");
  await page.goto(inOrg(`/workflows/${flow.id}/edit`));
  const fields = page.getByRole("group", { name: "Invocation and limits" });
  await fields.getByRole("spinbutton", { name: "Cost per workflow run (£)" }).fill("250");
  await expect(fields.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(inOrg(`/workflows/${flow.id}`));
  expect(flow.limits).toEqual({ run_cost: 250 });
});

test("an admin reads usage totals by agent, then by day", async ({ page }) => {
  const api = await fakeApi(page);
  const row = { embedded_texts: 0, web_searches: 0, sandbox_seconds: 0, unpriced: 0 };
  api.usage.agent = [
    { ...row, key: 1, name: "Researcher", model_calls: 12, input_tokens: 34_000, output_tokens: 2_100, cost: 1.25 },
    { ...row, key: null, name: null, model_calls: 2, input_tokens: 800, output_tokens: 40, cost: 0.0031 },
  ];
  api.usage.day = [{ ...row, key: "2026-10-13", name: "2026-10-13", model_calls: 14, input_tokens: 34_800, output_tokens: 2_140, cost: null, unpriced: 14 }];
  await page.goto(inOrg("/organisation/usage"));

  const table = page.getByRole("table", { name: "Usage totals" });
  await expect(table.getByRole("row")).toHaveCount(4);
  await expect(table.getByRole("row", { name: /Researcher/ })).toContainText("34,000");
  await expect(table.getByRole("row", { name: /Researcher/ })).toContainText("£1.25");
  await expect(table.getByRole("row", { name: /No agent/ })).toContainText("£0.0031");
  await expect(page.getByTestId("usage-total")).toHaveText("£1.25");

  await page.getByRole("combobox", { name: "Group usage" }).selectOption("day");
  await expect(table.getByRole("row", { name: /2026-10-13/ })).toContainText("No price");
  expect(api.usageReads.at(-1)).toContain("group=day");
});

test("only admins have the Usage tab", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  await page.goto(inOrg("/organisation"));
  await expect(page.getByRole("tab", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Usage" })).toHaveCount(0);
});
