import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

test("a workflow's page lists its schedules under a Schedules heading", async ({ page }) => {
  const api = await fakeApi(page);
  const workflow = api.addWorkflow("Weekly report");
  await page.goto(`/workflows/${workflow.id}`);

  await expect(page.getByRole("heading", { name: "Schedules" })).toBeVisible();
  await expect(
    page.getByText("No schedules. Add one to run this workflow automatically."),
  ).toBeVisible();
});
