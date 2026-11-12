import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * **Chat with this agent**: the button on an agent's page makes a new open chat with that agent
 * only, named "<Agent> – <person> – <date>" (" (2)" the second time), opens it with a mention of
 * the agent in the composer, and the agent's reply streams in. Every chat says the whole
 * organisation can see it; its members rename it. Someone the agent's invocable-by leaves out is
 * refused with the reason.
 */

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const chatWith = (page: Page) => page.getByRole("button", { name: "Chat with this agent" });
// The fake API's clock: everything happens on 14 October 2026.
const NAME = "Researcher – Terry Tester – 2026-10-14";

test("Chat with this agent makes a named chat with the agent, whose reply streams in", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  api.agentTurns.set(agent.id, {
    steps: 3,
    tokensPerStep: 100,
    drafts: ["Two", "Two papers"],
    reply: "Two papers found.",
  });
  await page.goto(inOrg(`/agents/${agent.id}`));

  await chatWith(page).click();

  await expect(page.getByRole("heading", { name: NAME })).toBeVisible();
  await expect(page).toHaveURL(/\/chats\/\d+$/);
  await expect(page.getByRole("note", { name: "Who can see this chat" })).toHaveText(
    "Everyone in tester can see this chat and read it, joined or not.",
  );
  // That agent only: no documents assistant.
  await page.getByRole("tab", { name: /Members/ }).click();
  await expect(page.getByRole("list", { name: "Agents" }).getByRole("listitem")).toHaveText(["Researcher"]);
  await page.getByRole("tab", { name: "Chat" }).click();

  // The composer starts with a mention of it, so the first message asks it.
  await expect(composer(page)).toHaveValue(`@[Researcher](agent:${agent.id}) `);
  await composer(page).press("End");
  await composer(page).pressSequentially("find papers");
  await composer(page).press("Enter");
  await expect(messageList(page).getByRole("article", { name: "Reply being written" })).toContainText("Two");
  await expect(messageList(page).getByRole("article").filter({ hasText: "Two papers found." })).toBeVisible({
    timeout: 10_000,
  });

  // Again the same day: a second chat, with " (2)".
  await page.goto(inOrg(`/agents/${agent.id}`));
  await chatWith(page).click();
  await expect(page.getByRole("heading", { name: `${NAME} (2)` })).toBeVisible();
});

test("its members rename it", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  await page.goto(inOrg(`/agents/${agent.id}`));
  await chatWith(page).click();
  await expect(page.getByRole("heading", { name: NAME })).toBeVisible();

  await page.getByRole("link", { name: "Edit" }).click();
  await page.getByLabel("Name").fill("Literature review");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page.getByRole("heading", { name: "Literature review" })).toBeVisible();
  await expect(page.getByRole("note", { name: "Who can see this chat" })).toBeVisible();
});

test("someone not allowed to invoke the agent is refused with the reason", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  const agent = api.addAgent("Payroll");
  agent.invocable_by = "editors";
  await page.goto(inOrg(`/agents/${agent.id}`));

  await chatWith(page).click();

  await expect(page.getByRole("alert")).toContainText("Only editors and admins can invoke the agent Payroll.");
  await expect(page).toHaveURL(new RegExp(`/agents/${agent.id}$`));
  expect(api.chats.size).toBe(0);
});

test("an archived agent offers no chat", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Old");
  Object.assign(agent, { archived_at: "2026-10-14T08:30:00Z", archived_by: api.me });
  await page.goto(inOrg(`/agents/${agent.id}`));
  await expect(page.getByRole("heading", { name: /Old/ })).toBeVisible();
  await expect(chatWith(page)).toHaveCount(0);
});
