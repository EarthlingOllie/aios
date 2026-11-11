import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Retention: how long runs and chat history are kept, on the Organisation page's Settings tab.
 * Both keep everything indefinitely by default; admins choose a number of days or switch back,
 * and editors and participants see the policy read-only.
 */

test("both settings keep everything indefinitely by default", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Retention" });
  await expect(section.getByRole("combobox", { name: "Keep runs" })).toHaveValue("forever");
  await expect(section.getByRole("combobox", { name: "Keep chat history" })).toHaveValue("forever");
  await expect(section.getByRole("spinbutton")).toHaveCount(0);
});

test("an admin keeps runs for a number of days, then indefinitely again", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Retention" });

  await section.getByRole("combobox", { name: "Keep runs" }).selectOption("days");
  const days = section.getByRole("spinbutton", { name: "Days to keep runs" });
  await days.fill("90");
  await section.getByRole("button", { name: "Apply" }).click();
  await expect(section.getByRole("status")).toHaveText("Saved");
  expect(api.writes).toEqual([["PUT", orgApi("/organisation/retention"), { run_days: 90, chat_days: null }]]);
  await page.reload();
  await expect(section.getByRole("spinbutton", { name: "Days to keep runs" })).toHaveValue("90");

  await section.getByRole("combobox", { name: "Keep runs" }).selectOption("forever");
  await section.getByRole("combobox", { name: "Keep chat history" }).selectOption("days");
  await section.getByRole("spinbutton", { name: "Days to keep chat history" }).fill("365");
  await section.getByRole("button", { name: "Apply" }).click();
  await expect(section.getByRole("status")).toHaveText("Saved");
  expect(api.writes.at(-1)).toEqual(["PUT", orgApi("/organisation/retention"), { run_days: null, chat_days: 365 }]);
});

test("days out of range can't be saved", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/settings"));
  const section = page.getByRole("region", { name: "Retention" });
  await section.getByRole("combobox", { name: "Keep chat history" }).selectOption("days");
  const days = section.getByRole("spinbutton", { name: "Days to keep chat history" });
  await days.fill("0");
  await expect(section.getByRole("button", { name: "Apply" })).toBeDisabled();
  await days.fill("36501");
  await expect(section.getByRole("button", { name: "Apply" })).toBeDisabled();
  await days.fill("30");
  await expect(section.getByRole("button", { name: "Apply" })).toBeEnabled();
  expect(api.writes).toEqual([]);
});

for (const role of ["editor", "participant"] as const) {
  test(`${role === "editor" ? "an" : "a"} ${role} sees the settings read-only`, async ({ page }) => {
    const api = await fakeApi(page);
    api.retention = { run_days: 30, chat_days: null };
    api.actAs(role);
    await page.goto(inOrg("/organisation/settings"));
    const section = page.getByRole("region", { name: "Retention" });
    await expect(section).toContainText("Runs are kept for 30 days after they finish.");
    await expect(section).toContainText("Chat history is kept indefinitely.");
    await expect(section.getByRole("combobox")).toHaveCount(0);
    await expect(section.getByRole("button")).toHaveCount(0);
    await expect(section).toContainText("Only admins can change");
  });
}
