import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Jev, the prompt-injection check: each document's verdict on the Data page's Documents, the
 * admins' flagged list on the Organisation page's Settings tab, where they release a document,
 * and the notice everyone sees while Jev is down.
 */

const REASON = "Jev judged this a likely prompt injection (probability 0.97).";
const documentRow = (page: Page, filename: string) =>
  page.getByRole("list", { name: "Documents" }).getByRole("listitem").filter({ hasText: filename });
const flaggedList = (page: Page) => page.getByRole("list", { name: "Flagged documents" });

test("the Documents tab shows each document's verdict, and why one was flagged", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("guide.md");
  api.addDocument("waiting.md", { verdict: "awaiting_check" });
  api.addDocument("email.md", { verdict: "flagged", verdict_reason: REASON });

  await page.goto(inOrg("/data/documents"));

  await expect(documentRow(page, "guide.md").getByLabel("Jev: Checked")).toBeVisible();
  await expect(documentRow(page, "waiting.md").getByLabel("Jev: Awaiting check")).toBeVisible();
  await expect(documentRow(page, "waiting.md")).toContainText("it isn't searchable until then");
  await expect(documentRow(page, "email.md").getByLabel("Jev: Flagged")).toBeVisible();
  await expect(documentRow(page, "email.md")).toContainText(REASON);
});

test("an admin sees the flagged list with Jev's reason, and releases a document", async ({ page }) => {
  const api = await fakeApi(page);
  api.addDocument("guide.md");
  const flagged = api.addDocument("email.md", { verdict: "flagged", verdict_reason: REASON });
  page.on("dialog", (dialog) => dialog.accept());

  await page.goto(inOrg("/organisation/settings"));

  const rows = flaggedList(page).getByRole("listitem");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("email.md");
  await expect(rows.first()).toContainText(REASON);

  await page.getByRole("button", { name: "Release email.md" }).click();

  await expect(page.getByRole("region", { name: "Flagged content" })).toContainText("Nothing is flagged.");
  expect(flagged.verdict).toBe("released");
  expect(api.auditEntries[0].action).toBe("document.released");
});

for (const role of ["editor", "participant"] as const) {
  test(`${role}s don't see the flagged list`, async ({ page }) => {
    const api = await fakeApi(page);
    api.addDocument("email.md", { verdict: "flagged", verdict_reason: REASON });
    api.actAs(role);

    await page.goto(inOrg("/organisation/settings"));

    await expect(page.getByRole("region", { name: "Approval rules" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Flagged content" })).toHaveCount(0);
  });
}

test("everyone sees a notice while Jev is down", async ({ page }) => {
  const api = await fakeApi(page);
  api.jevUp = false;

  await page.goto(inOrg("/chats"));

  await expect(page.getByRole("status", { name: "Jev is down" })).toContainText(
    "New documents wait to be checked before they're searchable",
  );
});

test("there's no notice while Jev is up", async ({ page }) => {
  await fakeApi(page);

  await page.goto(inOrg("/chats"));

  await expect(page.getByRole("heading", { name: "Chats" }).first()).toBeVisible();
  await expect(page.getByRole("status", { name: "Jev is down" })).toHaveCount(0);
});
