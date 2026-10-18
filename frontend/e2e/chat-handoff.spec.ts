import { expect, test, type Page } from "@playwright/test";

import { fakeApi } from "./fake-api";

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const message = (page: Page, text: string | RegExp) =>
  messageList(page).getByRole("article").filter({ hasText: text });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });

async function post(page: Page, text: string) {
  await composer(page).fill(text);
  await composer(page).press("Enter");
}

test("an agent's acknowledgement says what it understood, and its card shows the brief it was given", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Scout");
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "agent", agent);
  api.addChatMessage(chat, "The Q3 figures are in the shared sheet.");
  api.handoffs.set(agent.id, {
    ack: "On it: I'll check the Q3 figures against the plan.",
    brief: "The team is reviewing Q3. The figures are in the shared sheet.",
  });
  await page.goto(`/chats/${chat.id}`);

  await post(page, `@[Scout](agent:${agent.id}) check them`);
  // The placeholder first, while the handoff reads the chat.
  const ack = message(page, "Reading the chat…");
  await expect(ack).toBeVisible();
  await expect(ack.getByTestId("run-card-status")).toHaveText("Briefing");

  // Then what the agent understood, in place of the placeholder, and the run going.
  const understood = message(page, "On it: I'll check the Q3 figures against the plan.");
  await expect(understood).toBeVisible();
  await expect(message(page, "Reading the chat…")).toHaveCount(0);
  await expect(understood.getByTestId("run-card-status")).toHaveText("Running");

  // The brief is behind a disclosure on the card.
  const brief = understood.getByTestId("run-card-brief");
  await expect(brief.getByText("The team is reviewing Q3.")).toBeHidden();
  await brief.getByText("Brief").click();
  await expect(brief.getByText("The team is reviewing Q3. The figures are in the shared sheet.")).toBeVisible();
  // Opening it isn't a click through to the run.
  await expect(page).toHaveURL(`/chats/${chat.id}`);

  // The run was told the brief, a blank line, then the message.
  const [run] = [...api.agentRuns.values()];
  const [first] = run.ui_messages as { parts: { text: string }[] }[];
  expect(first.parts[0].text).toBe(
    "The team is reviewing Q3. The figures are in the shared sheet.\n\n@Scout check them",
  );
  // Named after what was asked, not the brief.
  expect(run.title).toBe("@Scout check them");
});

test("a workflow's card shows its brief too, and a failed handoff fails the card with why", async ({ page }) => {
  const api = await fakeApi(page);
  const digest = api.addWorkflow("Digest", [{ key: "one", agent: api.addAgent("Echo") }]);
  const report = api.addWorkflow("Report", [{ key: "one", agent: api.addAgent("Writer") }]);
  const chat = api.addChat("Design");
  api.addChatTarget(chat, "workflow", digest);
  api.addChatTarget(chat, "workflow", report);
  api.handoffs.set(digest.id, { ack: "I'll sum up the week.", brief: "The release is on Friday." });
  api.handoffs.set(report.id, { error: "model overloaded" });
  await page.goto(`/chats/${chat.id}`);

  await post(page, `@[Digest](workflow:${digest.id}) sum it up`);
  const digestAck = message(page, "I'll sum up the week.");
  await expect(digestAck).toBeVisible();
  await digestAck.getByTestId("run-card-brief").getByText("Brief").click();
  await expect(digestAck.getByText("The release is on Friday.")).toBeVisible();

  await post(page, `@[Report](workflow:${report.id}) write it up`);
  await expect(message(page, "Couldn't start the run: The handoff failed: model overloaded").first()).toBeVisible();
  const failed = message(page, "Reading the chat…");
  await expect(failed.getByTestId("run-card-status")).toHaveText("Failed");
  await expect(failed.getByRole("alert")).toHaveText("Couldn't start the run: The handoff failed: model overloaded");
  await expect(failed.getByTestId("run-card-brief")).toHaveCount(0);
});
