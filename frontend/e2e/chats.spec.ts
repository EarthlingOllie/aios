import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

test("creates a chat and opens it", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/chats"));
  await expect(page.getByText("No chats yet")).toBeVisible();

  await page.getByRole("link", { name: "New" }).click();
  await expect(page.getByRole("heading", { name: "New chat" })).toBeVisible();
  await page.getByLabel("Name").fill("Design");
  await page.getByLabel("Description").fill("Where design happens");
  await page.getByRole("button", { name: "Create chat" }).click();

  const [chat] = [...api.chats.values()];
  await expect(page).toHaveURL(inOrg(`/chats/${chat.id}`));
  expect(api.writes).toEqual([
    ["POST", orgApi("/chats"), { name: "Design", description: "Where design happens" }],
  ]);
  // The header: name and description (the list shows the description too).
  const heading = page.getByRole("heading", { name: "Design", level: 2 });
  await expect(heading).toBeVisible();
  await expect(page.getByLabel("Visibility")).toHaveCount(0);
  await expect(heading.locator("..")).toContainText("Where design happens");
  await expect(heading.locator("..")).toContainText("2 members");
  // The creator is its only user; the organisation assistant, its default chat agent, is its agent.
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByText("Terry Tester", { exact: true })).toBeVisible();
  await expect(members.getByText("owner", { exact: true })).toHaveCount(0);
  await expect(members.getByRole("list", { name: "Agents" })).toHaveText("Organisation assistant");
  await expect(members.getByText("No workflows yet")).toBeVisible();
  // And it's in the list.
  await expect(page.getByRole("list").filter({ hasText: "Design" }).getByText("2 members")).toBeVisible();
});

test("a member removes an agent, but nobody removes a person", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  api.addChatMember(chat, { id: 2, first_name: "Sam", last_name: "Jones" });
  const agent = api.addAgent("Scout");
  const member = api.addChatTarget(chat, "agent", agent);
  await page.goto(inOrg(`/chats/${chat.id}`));

  await expect(page.getByRole("heading", { name: "Design", level: 2 })).toBeVisible();
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByRole("list", { name: "Users" }).getByRole("listitem")).toHaveText([
    /Terry Tester/,
    /Sam Jones/,
  ]);
  await expect(members.getByRole("button", { name: "Remove Sam Jones" })).toHaveCount(0);
  await expect(members.getByRole("button", { name: "Remove Terry Tester" })).toHaveCount(0);
  // Nor does anyone add a person: people join themselves.
  await expect(members.getByLabel("Add a member")).toHaveCount(0);
  page.on("dialog", (dialog) => dialog.accept());
  await members.getByRole("button", { name: "Remove Scout" }).click();
  await expect(members.getByRole("list", { name: "Agents" })).toHaveCount(0);
  expect(api.writes).toEqual([["DELETE", orgApi(`/chats/${chat.id}/members/${member.id}`), null]]);
});

test("someone who hasn't joined sees no members' actions", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design", { owner: { id: 2, first_name: "Sam", last_name: "Jones" } });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await expect(page.getByRole("heading", { name: "Design" })).toBeVisible();
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByRole("listitem")).toHaveText([/Sam Jones/]);
  await expect(page.getByRole("link", { name: "Edit" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);
  await expect(members.getByRole("button", { name: /Remove/ })).toHaveCount(0);
});
