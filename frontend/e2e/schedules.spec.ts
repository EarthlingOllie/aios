import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The new-schedule form's preview, as the timezone is typed. */

test.use({ timezoneId: "Europe/London" });

async function openForm(page: Page) {
  await fakeApi(page);
  await page.goto("/scheduler");
  await page.getByRole("button", { name: "Add schedule" }).click();
  await expect(page.getByTestId("schedule-preview")).toContainText("Next runs (Europe/London)");
  return page.getByLabel("Timezone");
}

test("a partly typed timezone doesn't break the preview", async ({ page }) => {
  const timezone = await openForm(page);
  await timezone.fill("America/New_York");
  await expect(page.getByTestId("schedule-preview")).toContainText("Next runs (America/New_York)");
  // Each keystroke renders before the debounced check: the preview fetched for the whole name
  // is still there, and must not be formatted in the half-typed one.
  await timezone.press("Backspace");
  await timezone.press("Backspace");
  await expect(page.getByTestId("schedule-preview-error")).toContainText(
    "Unknown timezone: 'America/New_Yo'",
  );
  await expect(page.getByRole("form", { name: "New schedule" })).toBeVisible();
  await timezone.press("r");
  await timezone.press("k");
  await expect(page.getByTestId("schedule-preview")).toContainText("Next runs (America/New_York)");
  await expect(page.getByTestId("schedule-preview")).toContainText("9:00");
});

test("a timezone the server knows but the browser doesn't is shown in UTC", async ({ page }) => {
  const timezone = await openForm(page);
  await timezone.fill("localtime");
  await expect(page.getByTestId("schedule-preview")).toContainText("in UTC");
  await expect(page.getByTestId("schedule-preview")).toContainText("1:00");
});

test("a schedule's agent or workflow opens on its Schedule tab", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  const workflow = api.addWorkflow("Report");
  api.addSchedule("agent", agent);
  api.addSchedule("workflow", workflow);
  await page.goto("/scheduler");

  await page.getByRole("link", { name: "Researcher" }).click();
  await expect(page).toHaveURL(`/agents/${agent.id}/schedule`);
  await expect(page.getByRole("tab", { name: "Schedule" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel").getByTestId("schedule-row")).toHaveCount(1);

  await page.goto("/scheduler");
  await page.getByRole("link", { name: "Report" }).click();
  await expect(page).toHaveURL(`/workflows/${workflow.id}/schedule`);
  await expect(page.getByRole("tab", { name: "Schedule" })).toHaveAttribute("aria-selected", "true");
});
