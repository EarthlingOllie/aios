import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

/**
 * Pressing `/` anywhere in a section jumps to its list's search box, unless you're already
 * typing somewhere, where it types a `/` as usual.
 */

const search = (page: Page, name: string) => page.getByRole("searchbox", { name: `Search ${name}` });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });

test("/ focuses the agents search and selects its text, without typing a /", async ({ page }) => {
  const api = await fakeApi(page);
  api.addAgent("Researcher");
  await page.goto("/agents");

  // The empty box says how to get to it.
  await expect(page.getByTitle("Press / to search")).toHaveText("/");

  await search(page, "agents").fill("res");
  await page.getByRole("link", { name: /Researcher/ }).click();
  await expect(page.getByRole("heading", { name: "Researcher" })).toBeVisible();

  await page.keyboard.press("/");
  await expect(search(page, "agents")).toBeFocused();
  // Its text is selected, so typing replaces it.
  await page.keyboard.type("zz");
  await expect(search(page, "agents")).toHaveValue("zz");

  // Pressed again in the box itself, it types.
  await page.keyboard.press("/");
  await expect(search(page, "agents")).toHaveValue("zz/");
});

test("/ types as usual in the chat composer", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  await page.goto(`/chats/${chat.id}`);

  await composer(page).click();
  await page.keyboard.type("a/b");
  await expect(composer(page)).toHaveValue("a/b");
  await expect(composer(page)).toBeFocused();
  await expect(search(page, "chats")).toHaveValue("");
});

test("/ types as usual in a form field", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/agents/new");

  const name = page.getByLabel("Name");
  await name.click();
  await page.keyboard.type("x/y");
  await expect(name).toHaveValue("x/y");
});

test("on a small screen showing only the item, / does nothing", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 800 });
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(`/agents/${agent.id}`);
  await expect(page.getByRole("heading", { name: "Researcher" })).toBeVisible();

  await page.keyboard.press("/");
  // The list, and its search box, are hidden.
  await expect(search(page, "agents")).toHaveCount(0);
  expect(await page.evaluate(() => document.activeElement?.tagName)).toBe("BODY");
});
