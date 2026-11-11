import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Organisations in links: every signed-in page is under `/o/<slug>/`, and a link without one
 * (an old bookmark) opens the same page in the organisation used last.
 */

const globex = { id: 2, name: "Globex", slug: "globex", is_creator: false };

test("an old bookmark opens the same page in the organisation, keeping its query and hash", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/agents?q=research#top");
  await expect(page).toHaveURL(inOrg("/agents?q=research#top"));
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
});

test("the root opens the organisation's landing page", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/");
  await expect(page).toHaveURL(new RegExp(`${inOrg("/")}(chats/new)?$`));
  await expect(page.getByRole("button", { name: "tester", exact: true }).first()).toBeVisible();
});

test("an old bookmark opens in the organisation used last", async ({ page }) => {
  const api = await fakeApi(page);
  // Globex was joined more recently, but tester was used last.
  api.otherMemberships = [globex];
  await page.goto(inOrg("/chats"));
  await expect(page.getByRole("button", { name: "tester", exact: true }).first()).toBeVisible();
  await page.goto("/agents");
  await expect(page).toHaveURL(inOrg("/agents"));
});

test("with none used yet, an old bookmark opens in the organisation joined most recently", async ({ page }) => {
  const api = await fakeApi(page);
  api.joinedEarlier = true;
  api.otherMemberships = [globex];
  await page.goto("/skills");
  await expect(page).toHaveURL(inOrg("/skills"));

  // Once Globex is the more recent, with nothing remembered, that's where one opens.
  await page.evaluate(() => localStorage.clear());
  api.joinedEarlier = false;
  await page.goto("/skills");
  await expect(page).toHaveURL("/o/globex/skills");
});

test("the app's own links stay in the organisation", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/agents"));
  await page.getByRole("link", { name: "Skills" }).first().click();
  await expect(page).toHaveURL(inOrg("/skills"));
  await page.getByRole("button", { name: "tester", exact: true }).first().click();
  await page.getByRole("menu", { name: "Organisations" }).getByRole("menuitem", { name: "tester" }).click();
  await expect(page).toHaveURL(inOrg("/organisation"));
});

test("a link to an organisation the user isn't in shows not found, as for one that doesn't exist", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.otherMemberships = [globex];
  for (const slug of ["initech", "nothing-here"]) {
    await page.goto(`/o/${slug}/agents`);
    await expect(page.getByRole("heading", { name: "Organisation not found" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Agents" })).toHaveCount(0);
  }
  await page.getByRole("link", { name: "Go to your organisation" }).click();
  await expect(page).toHaveURL("/o/globex/");
  await expect(page.getByRole("link", { name: "Agents" }).first()).toBeVisible();
});
