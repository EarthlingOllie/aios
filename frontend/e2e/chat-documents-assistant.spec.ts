import { expect, test } from "@playwright/test";

import { fakeApi } from "./fake-api";

/** The documents assistant joins each new chat, and `/` opens the Chats section. */

test("a new chat has the documents assistant as a member, to mention like any agent", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto("/chats/new");
  await page.getByLabel("Name").fill("Design");
  await page.getByRole("button", { name: "Create chat" }).click();

  const [chat] = [...api.chats.values()];
  await expect(page).toHaveURL(`/chats/${chat.id}`);
  const assistant = api.documentsAssistant();
  api.agentTurns.set(assistant.id, { steps: 1, tokensPerStep: 100, reply: "Your notes are about fish." });
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByRole("list", { name: "Agents" }).getByRole("listitem")).toHaveText([
    "Documents assistant",
  ]);
  await expect(page.getByRole("heading", { name: "Design", level: 2 }).locator("..")).toContainText("2 members");
  await page.getByRole("tab", { name: "Chat" }).click();

  const composer = page.getByRole("textbox", { name: "Message", exact: true });
  await composer.pressSequentially("@doc");
  const suggestions = page.getByRole("listbox", { name: "Mention" });
  await expect(suggestions.getByRole("group")).toHaveText([/Agents.*Documents assistant/]);
  await composer.press("Enter");
  await composer.pressSequentially("what's in my notes?");
  await composer.press("Enter");

  // Mentioned like any agent, it answers as one: its acknowledgement and card, then its reply.
  await expect.poll(() => api.writes.at(-1)).toEqual([
    "POST",
    `/api/chats/${chat.id}/messages`,
    {
      body: `@[Documents assistant](agent:${assistant.id}) what's in my notes?`,
      mentions: [{ kind: "agent", id: assistant.id }],
    },
  ]);
  const log = page.getByRole("log", { name: "Messages" });
  await expect(log.getByTestId("run-card")).toBeVisible();
  await expect(log).toContainText("On it.");
  const reply = log.getByRole("article").filter({ hasText: "Your notes are about fish." });
  await expect(reply.getByText("Documents assistant", { exact: true })).toBeVisible({ timeout: 10_000 });
});

test("/ opens the most recent chat I'm in", async ({ page }) => {
  const api = await fakeApi(page);
  api.addChat("Older");
  const newer = api.addChat("Newer");
  // A public chat I'm not in isn't one to land on.
  api.addChat("Theirs", { visibility: "public", owner: { id: 2, login: "sam", name: "Sam" } });
  await page.goto("/");

  await expect(page).toHaveURL(`/chats/${newer.id}`);
  await expect(page.getByRole("heading", { name: "Newer", level: 2 })).toBeVisible();
  // There's one place to chat.
  await expect(page.getByRole("link", { name: "Chat", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Chats" }).first()).toHaveClass(/font-medium/);
});

test("/ opens the new-chat form when I'm in no chat yet", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/");

  await expect(page).toHaveURL("/chats/new");
  await expect(page.getByRole("heading", { name: "New chat" })).toBeVisible();
});
