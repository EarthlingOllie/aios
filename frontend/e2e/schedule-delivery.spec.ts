import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

test("a schedule can deliver its results to a chat, chosen on its form", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Newsbot");
  api.addChat("Ops");
  await page.goto(inOrg("/scheduler"));

  await page.getByRole("button", { name: "Add schedule" }).click();
  const form = page.getByRole("form", { name: "New schedule" });
  await form.getByLabel("Runs").selectOption({ label: "Newsbot" });
  await form.getByLabel("Message").fill("What happened today?");
  await expect(form.getByLabel("Deliver to")).toHaveValue("");
  await form.getByLabel("Deliver to").selectOption({ label: "Ops" });
  await form.getByRole("button", { name: "Create schedule" }).click();

  const row = page.getByTestId("schedule-row");
  await expect(row.getByTestId("deliver-to")).toContainText("Delivers to Ops");
  expect(api.schedules[0].target_id).toBe(agent.id);
  expect((api.schedules[0].deliver_to as { name: string }).name).toBe("Ops");
});

test("choosing an archived chat is refused with why", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Newsbot");
  const chat = api.addChat("Ops");
  await page.goto(inOrg(`/agents/${agent.id}/schedule`));
  await page.getByRole("button", { name: "Add schedule" }).click();
  const form = page.getByRole("form", { name: "New schedule" });
  await form.getByLabel("Message").fill("What happened today?");
  await form.getByLabel("Deliver to").selectOption({ label: "Ops" });
  // Archived while the form was open.
  chat.archived_at = "2026-11-01T09:00:00Z";
  await form.getByRole("button", { name: "Create schedule" }).click();

  await expect(form.getByRole("alert").first()).toContainText(
    "Ops is archived, so results can't be delivered there. Choose another chat.",
  );
});

test("a schedule whose chat was archived says why nothing was delivered, and the form asks for another", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Newsbot");
  const chat = api.addChat("Ops");
  api.addChat("Ops 2");
  api.addSchedule("agent", agent);
  Object.assign(api.schedules[0], {
    deliver_to: { id: chat.id, name: "Ops", archived: true },
    delivery_problem:
      "Ops is archived, so the result wasn't delivered. Choose another chat.",
  });
  chat.archived_at = "2026-11-01T09:00:00Z";
  await page.goto(inOrg("/scheduler"));

  const row = page.getByTestId("schedule-row");
  await expect(row.getByTestId("deliver-to")).toContainText(
    "Delivers to Ops (archived)",
  );
  await expect(row.getByTestId("delivery-problem")).toHaveText(
    "Ops is archived, so the result wasn't delivered. Choose another chat.",
  );
  await row.getByRole("button", { name: "Edit schedule" }).click();
  const form = page.getByRole("form", { name: "Edit schedule" });
  await expect(
    form.getByText(
      "Ops is archived, so results aren't delivered there. Choose another chat.",
    ),
  ).toBeVisible();
  await form.getByLabel("Deliver to").selectOption({ label: "Ops 2" });
  await form.getByRole("button", { name: "Save schedule" }).click();
  await expect(row.getByTestId("deliver-to")).toContainText(
    "Delivers to Ops 2",
  );
  await expect(row.getByTestId("delivery-problem")).toHaveCount(0);
});
