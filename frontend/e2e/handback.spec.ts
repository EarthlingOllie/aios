import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const message = (page: Page, text: string | RegExp) =>
  messageList(page).getByRole("article").filter({ hasText: text });
const composer = (page: Page) =>
  page.getByRole("textbox", { name: "Message", exact: true });

async function post(page: Page, text: string) {
  await composer(page).fill(text);
  await composer(page).press("Enter");
}

test("an agent's JSON reply is summarised, with the full output pretty-printed behind the card's disclosure", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Counter");
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "agent", agent);
  api.agentTurns.set(agent.id, {
    steps: 1,
    tokensPerStep: 120,
    reply: '{"total":3,"items":["a","b","c"]}',
  });
  api.handbacks.set(agent.id, { summary: "Three items: a, b and c." });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await post(page, `@[Counter](agent:${agent.id}) count them`);
  const card = message(page, "On it.");
  // While the handback summarises the run's output, the card says so.
  await expect(card.getByTestId("run-card-summarising")).toContainText(
    "Summarising",
  );

  // Then the summary is posted, not the raw JSON.
  // (A beat of the fake's per read: the reads come every few seconds once nothing streams.)
  await expect(message(page, "Three items: a, b and c.")).toBeVisible({
    timeout: 20_000,
  });
  await expect(card.getByTestId("run-card-summarising")).toHaveCount(0);
  await expect(messageList(page).getByText('{"total":3')).toHaveCount(0);

  // The full output is one click away on the card, pretty-printed.
  const full = card.getByTestId("run-card-full-output");
  await expect(full.locator("pre")).toHaveCount(0);
  await full.getByText("Full output").click();
  await expect(full.locator("pre")).toHaveText(
    '{\n  "total": 3,\n  "items": [\n    "a",\n    "b",\n    "c"\n  ]\n}',
  );
  await expect(page).toHaveURL(inOrg(`/chats/${chat.id}`));
});

test("an agent's short reply is posted as written, with no full output to show", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Scout");
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "agent", agent);
  api.agentTurns.set(agent.id, {
    steps: 1,
    tokensPerStep: 120,
    reply: "All good.",
  });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await post(page, `@[Scout](agent:${agent.id}) check`);
  await expect(message(page, "All good.")).toBeVisible();
  await expect(
    messageList(page).getByTestId("run-card-full-output"),
  ).toHaveCount(0);
});

test("a handback that fails posts the raw output with a note", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Counter");
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "agent", agent);
  api.agentTurns.set(agent.id, {
    steps: 1,
    tokensPerStep: 120,
    reply: "[1, 2]",
  });
  api.handbacks.set(agent.id, { error: "model overloaded" });
  await page.goto(inOrg(`/chats/${chat.id}`));

  await post(page, `@[Counter](agent:${agent.id}) count`);
  await expect(
    message(page, /summarise this result \(model overloaded\)/),
  ).toContainText("[1, 2]", {
    timeout: 20_000,
  });
});

test("a delivered result shows its summary with the run's card collapsed under it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Newsbot");
  const chat = api.addChat("Ops");
  api.addChatTarget(chat, "agent", agent);
  api.addDelivery(
    chat,
    "agent",
    agent,
    "Three stories today.",
    "Story one…\nStory two…\nStory three…",
  );
  await page.goto(inOrg(`/chats/${chat.id}`));

  const delivered = message(page, "Three stories today.");
  await expect(delivered).toBeVisible();
  const run = delivered.getByTestId("delivered-run");
  await expect(run.getByTestId("run-card")).toBeHidden();
  await run.getByText("Run", { exact: true }).click();
  await expect(run.getByTestId("run-card")).toBeVisible();
  await run
    .getByTestId("run-card-full-output")
    .getByText("Full output")
    .click();
  await expect(run.getByText("Story three…")).toBeVisible();
});
