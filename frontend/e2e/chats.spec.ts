import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

test("creates a chat and opens it", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/chats");
  await expect(page.getByText("No chats yet")).toBeVisible();

  await page.getByRole("link", { name: "New" }).click();
  await expect(page.getByRole("heading", { name: "New chat" })).toBeVisible();
  await page.getByLabel("Name").fill("Design");
  await page.getByLabel("Description").fill("Where design happens");
  await page.getByLabel("Visibility").selectOption("public");
  await page.getByRole("button", { name: "Create chat" }).click();

  const [chat] = [...api.chats.values()];
  await expect(page).toHaveURL(`/chats/${chat.id}`);
  expect(api.writes).toEqual([
    ["POST", "/api/chats", { name: "Design", description: "Where design happens", visibility: "public" }],
  ]);
  // The header: name, visibility and description (the list shows the description too).
  const heading = page.getByRole("heading", { name: "Design", level: 2 });
  await expect(heading).toBeVisible();
  await expect(heading).toContainText("Public");
  await expect(heading.locator("..")).toContainText("Where design happens");
  await expect(heading.locator("..")).toContainText("2 members");
  // The creator is the owner and only user; their documents assistant is its first agent.
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByText("Tester", { exact: true })).toBeVisible();
  await expect(members.getByText("owner", { exact: true })).toBeVisible();
  await expect(members.getByRole("list", { name: "Agents" })).toHaveText("Documents assistant");
  await expect(members.getByText("No workflows yet")).toBeVisible();
  // And it's in the list.
  await expect(page.getByRole("list").filter({ hasText: "Design" }).getByText("2 members")).toBeVisible();
});

test("the owner removes a member", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design", { visibility: "private" });
  const member = api.addChatMember(chat, { id: 2, login: "sam", name: "Sam" });
  await page.goto(`/chats/${chat.id}`);

  await expect(page.getByRole("heading", { name: "Design", level: 2 })).toContainText("Private");
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByRole("listitem")).toHaveText([/Tester.*owner/, /Sam/]);
  page.on("dialog", (dialog) => dialog.accept());
  await members.getByRole("button", { name: "Remove Sam" }).click();
  await expect(members.getByRole("listitem")).toHaveText([/Tester.*owner/]);
  expect(api.writes).toEqual([["DELETE", `/api/chats/${chat.id}/members/${member.id}`, null]]);
});

test("a member sees no owner actions", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design", { owner: { id: 2, login: "sam", name: "Sam" } });
  api.addChatMember(chat, api.me);
  await page.goto(`/chats/${chat.id}`);

  await expect(page.getByRole("heading", { name: "Design" })).toBeVisible();
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByRole("listitem")).toHaveText([/Sam.*owner/, /Tester/]);
  await expect(page.getByRole("link", { name: "Edit" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);
  await expect(members.getByRole("button", { name: /Remove/ })).toHaveCount(0);
});
