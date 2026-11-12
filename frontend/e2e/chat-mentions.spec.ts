import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type ChatUser, inOrg, orgApi } from "./fake-api";

const sam: ChatUser = { id: 2, first_name: "Sam", last_name: "Jones" };

const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
const message = (page: Page, text: string | RegExp) =>
  messageList(page).getByRole("article").filter({ hasText: text });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const suggestions = (page: Page) => page.getByRole("listbox", { name: "Mention" });

test("a member adds the organisation's agents and workflows, grouped by kind", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  const agent = api.addAgent("Review");
  const workflow = api.addWorkflow("Review");
  api.addWorkflow("Digest");
  await page.goto(inOrg(`/chats/${chat.id}`));

  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByText("No agents yet")).toBeVisible();
  await members.getByLabel("Add an agent").selectOption("Review");
  await members.getByRole("button", { name: "Add agent" }).click();
  const agents = members.getByRole("list", { name: "Agents" });
  await expect(agents.getByRole("listitem")).toHaveText(["Review"]);
  await expect(agents.getByRole("img", { name: "Agent" })).toBeVisible();

  await members.getByLabel("Add a workflow").selectOption("Review");
  await members.getByRole("button", { name: "Add workflow" }).click();
  const workflows = members.getByRole("list", { name: "Workflows" });
  await expect(workflows.getByRole("listitem")).toHaveText(["Review"]);
  await expect(workflows.getByRole("img", { name: "Workflow" })).toBeVisible();
  // What's already in the chat isn't offered again.
  await expect(members.getByLabel("Add a workflow").getByRole("option")).toHaveText(["Choose a workflow", "Digest"]);
  await expect(page.getByText("3 members").first()).toBeVisible();
  expect(api.writes).toEqual([
    ["POST", orgApi(`/chats/${chat.id}/members`), { kind: "agent", id: agent.id }],
    ["POST", orgApi(`/chats/${chat.id}/members`), { kind: "workflow", id: workflow.id }],
  ]);
});

test("someone who hasn't joined can't add agents or workflows", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design", { owner: sam });
  api.addAgent("Review");
  await page.goto(inOrg(`/chats/${chat.id}`));
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByText("No agents yet")).toBeVisible();
  await expect(members.getByLabel("Add an agent")).toHaveCount(0);
});

test("@ offers the chat's members by kind, then the agents it doesn't include, and inserts a chip", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  api.addChatMember(chat, sam);
  const agent = api.addAgent("Review");
  const workflow = api.addWorkflow("Review");
  api.addChatTarget(chat, "agent", agent);
  api.addChatTarget(chat, "workflow", workflow);
  // Not in the chat, so offered after its own: mentioning it is refused, offering to add it.
  api.addAgent("Outsider");
  await page.goto(inOrg(`/chats/${chat.id}`));
  await expect(messageList(page)).toContainText("No messages yet");

  await composer(page).pressSequentially("Please @");
  const list = suggestions(page);
  await expect(list.getByRole("group")).toHaveText([
    /Users.*Terry Tester.*Sam Jones/,
    /Agents.*Review.*Outsider/,
    /Workflows.*Review/,
  ]);
  await composer(page).pressSequentially("rev");
  // An agent and a workflow with the same name, told apart by kind.
  await expect(list.getByRole("option")).toHaveText(["Review agent", "Review workflow"]);
  await expect(list.getByRole("option", { name: "Review agent" })).toHaveAttribute("aria-selected", "true");
  await composer(page).press("ArrowDown");
  await expect(list.getByRole("option", { name: "Review workflow" })).toHaveAttribute("aria-selected", "true");
  await composer(page).press("Enter");
  await expect(list).toHaveCount(0);
  await expect(composer(page)).toHaveValue(`Please @[Review](workflow:${workflow.id}) `);

  // A user is found by their last name (or first), whatever its case; picking by mouse works as well.
  await composer(page).pressSequentially("and @JON");
  await expect(list.getByRole("option")).toHaveText(["Sam Jones user"]);
  await list.getByRole("option", { name: "Sam Jones user" }).click();
  await composer(page).pressSequentially("thanks");
  await composer(page).press("Enter");

  const posted = message(page, "thanks");
  await expect(posted).toBeVisible();
  const chip = posted.getByTitle(`Workflow: Review`);
  await expect(chip).toHaveText("Review");
  await expect(chip.getByRole("img", { name: "Workflow" })).toBeVisible();
  await expect(posted.getByTitle("User: Sam Jones")).toHaveText("Sam Jones");
  // Not the raw token.
  await expect(posted).not.toContainText("workflow:");
  expect(api.writes).toEqual([
    [
      "POST",
      orgApi(`/chats/${chat.id}/messages`),
      {
        body: `Please @[Review](workflow:${workflow.id}) and @[Sam Jones](user:2) thanks`,
        mentions: [
          { kind: "workflow", id: workflow.id },
          { kind: "user", id: 2 },
        ],
      },
    ],
  ]);
});

test("Escape closes the suggestions, and Enter then sends", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  await page.goto(inOrg(`/chats/${chat.id}`));
  await composer(page).pressSequentially("mail @tes");
  await expect(suggestions(page).getByRole("option")).toHaveText(["Terry Tester user"]);
  await composer(page).press("Escape");
  await expect(suggestions(page)).toHaveCount(0);
  await composer(page).press("Enter");
  await expect(message(page, "mail @tes")).toBeVisible();
  expect(api.writes).toEqual([["POST", orgApi(`/chats/${chat.id}/messages`), { body: "mail @tes", mentions: [] }]]);
});

test("a token for someone who isn't a member is plain text, not a mention", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  await page.goto(inOrg(`/chats/${chat.id}`));
  const typed = `ping @[Outsider](user:${sam.id})`;
  await composer(page).fill(typed);
  await composer(page).press("Enter");
  const posted = message(page, "ping");
  await expect(posted).toContainText("@Outsider");
  await expect(posted.getByTitle(/Outsider/)).toHaveCount(0);
  expect(api.writes).toEqual([["POST", orgApi(`/chats/${chat.id}/messages`), { body: typed, mentions: [] }]]);
});

test("a message that mentions me is highlighted", async ({ page }) => {
  const api = await fakeApi(page);
  const chat = api.addChat("Design");
  api.addChatMember(chat, sam);
  api.addChatMessage(chat, "@[Terry Tester](user:1) can you look?", sam, [{ kind: "user", id: 1, name: "Terry Tester" }]);
  api.addChatMessage(chat, "@[Sam Jones](user:2) and you?", api.me, [{ kind: "user", id: 2, name: "Sam Jones" }]);
  await page.goto(inOrg(`/chats/${chat.id}`));

  const mine = message(page, "can you look?");
  await expect(mine.getByTitle("User: Terry Tester")).toHaveText("Terry Tester");
  await expect(mine).toContainText("Mentions you");
  await expect(message(page, "and you?")).not.toContainText("Mentions you");
});
