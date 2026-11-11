import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Mention guards in a chat: a message mentioning too many agents and workflows is refused (422),
 * as is mentioning too often (429), and either way the composer keeps the draft; a mention the
 * poster may not make (the target's invocable-by) posts with a refused card. Participants mention
 * like anyone else.
 */

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });

test("a message mentioning too many agents is refused, and the composer keeps the draft", async ({ page }) => {
  const api = await fakeApi(page);
  api.mentionsPerMessage = 2;
  const chat = api.addChat("Ops");
  const bots = ["Ada", "Bea", "Cy"].map((name) => api.addAgent(name));
  for (const bot of bots) api.addChatTarget(chat, "agent", bot);
  await page.goto(inOrg(`/chats/${chat.id}`));

  const text = bots.map((b) => `@[${b.name}](agent:${b.id})`).join(" ") + " all of you";
  await composer(page).fill(text);
  await composer(page).press("Enter");

  await expect(page.getByRole("alert")).toContainText("A message can mention at most 2 agents and workflows");
  await expect(composer(page)).toHaveValue(text);
  await expect(messageList(page).getByRole("article")).toHaveCount(0);
});

test("mentioning too often is refused with when to try again, and the draft is kept", async ({ page }) => {
  const api = await fakeApi(page);
  api.mentionRate = 0;
  api.mentionRetryAfter = 37;
  const chat = api.addChat("Ops");
  const bot = api.addAgent("Helper");
  api.addChatTarget(chat, "agent", bot);
  await page.goto(inOrg(`/chats/${chat.id}`));

  const text = `@[Helper](agent:${bot.id}) again`;
  await composer(page).fill(text);
  await composer(page).press("Enter");

  await expect(page.getByRole("alert")).toContainText("Try again in 37 seconds");
  await expect(composer(page)).toHaveValue(text);
  await expect(messageList(page).getByRole("article")).toHaveCount(0);
});

test("a participant's mention of an agent only editors may invoke posts with a refused card", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  const chat = api.addChat("Ops");
  const payroll = api.addAgent("Payroll");
  payroll.invocable_by = "editors";
  const helper = api.addAgent("Helper");
  api.addChatTarget(chat, "agent", payroll);
  api.addChatTarget(chat, "agent", helper);
  await page.goto(inOrg(`/chats/${chat.id}`));

  await composer(page).fill(`@[Payroll](agent:${payroll.id}) and @[Helper](agent:${helper.id}) please`);
  await composer(page).press("Enter");

  const refused = messageList(page).getByTestId("run-card-refused");
  await expect(refused).toHaveText("Not run: Only editors and admins can invoke the agent Payroll.");
  await expect(messageList(page).getByText("Refused")).toBeVisible();
  // The message itself is posted, and the other mention goes ahead.
  await expect(messageList(page).getByRole("article").filter({ hasText: "please" }).first()).toBeVisible();
  await expect(messageList(page).getByTestId("run-card")).toHaveCount(2);
  await expect(composer(page)).toHaveValue("");
});
