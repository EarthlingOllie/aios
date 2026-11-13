import { expect, test } from "@playwright/test";

import { composer, created, mention, messageList, register, reply, setUp, uniqueSuffix } from "./stack";

/**
 * Phase 2's chats against the real stack, on the scripted model
 * (backend/src/aios/testing/scenarios.py):
 *
 * - **Chat with this agent** on an agent's page makes a chat with that agent only, its composer
 *   starting with a mention of it; the agent (told to write as it goes) streams its reply into
 *   the browser a piece at a time, as a draft that the posted reply replaces.
 * - A workflow mentioned in a chat replies in JSON; the handback posts its short summary as the
 *   result, and the full output, pretty-printed, is behind the card's disclosure.
 * - A member invited into the organisation finds a chat they haven't joined, reads it without
 *   joining, and posting in it joins them.
 */

test("Chat with this agent makes a chat with that agent, whose reply streams in", async ({ page }) => {
  const { api, link } = await setUp(page);
  const agent = await created<{ id: number }>(page.request, api("/agents"), {
    name: "Researcher",
    base_prompt: "Write as you go.",
  });
  await page.goto(link(`/agents/${agent.id}`));

  await page.getByRole("button", { name: "Chat with this agent" }).click();

  // A chat named for the agent and the person, open to the organisation, with that agent only.
  await expect(page.getByRole("heading", { name: /^Researcher – Core Loop – \d{4}-\d{2}-\d{2}$/ })).toBeVisible();
  await expect(page).toHaveURL(/\/chats\/\d+$/);
  await expect(page.getByRole("note", { name: "Who can see this chat" })).toContainText("can see this chat");
  await page.getByRole("tab", { name: /Members/ }).click();
  await expect(page.getByRole("list", { name: "Agents" }).getByRole("listitem")).toHaveText(["Researcher"]);
  await page.getByRole("tab", { name: "Chat" }).click();

  // Its composer starts with a mention of the agent, so the first message asks it.
  await expect(composer(page)).toHaveValue(`@[Researcher](agent:${agent.id}) `);
  await composer(page).press("End");
  await composer(page).pressSequentially("find papers on streaming");
  await composer(page).press("Enter");

  // The reply streams in: a draft, written a piece at a time, ...
  const draft = messageList(page).getByRole("article", { name: "Reply being written" });
  await expect(draft).toContainText("Here are three papers", { timeout: 60_000 });
  await expect(draft).not.toContainText("A, B and C.");
  // ... which the posted reply replaces, once.
  const whole = "Here are three papers on streaming: A, B and C.";
  await expect(reply(page, whole)).toHaveCount(1, { timeout: 30_000 });
  await expect(draft).toHaveCount(0);
  await expect(reply(page, whole).getByText("Researcher", { exact: true })).toBeVisible();
  await expect(messageList(page).getByTestId("run-card-status")).toHaveText("Succeeded");
});

test("a mentioned workflow's result is handed back as a summary, with the full output behind it", async ({
  page,
}) => {
  const { api, link, chat } = await setUp(page);
  // On the scripted model, an agent told "Reply with: <text>" replies <text>; the handback sums a
  // JSON object up as its fields.
  const output = '{"papers": ["A", "B", "C"], "found": 3}';
  const lister = await created<{ current_version: { id: number } }>(page.request, api("/agents"), {
    name: "Lister",
    base_prompt: `Reply with: ${output}`,
  });
  const workflow = await created<{ id: number }>(page.request, api("/workflows"), {
    name: "Survey",
    nodes: [{ key: "list", kind: "agent", version_id: lister.current_version.id }],
    edges: [],
  });
  await created(page.request, api(`/chats/${chat.id}/members`), { kind: "workflow", id: workflow.id });
  await page.goto(link(`/chats/${chat.id}`));

  await mention(page, "list the papers", "Survey", "workflow");

  // The summary is posted as the workflow's result, not its raw JSON.
  const summary = reply(page, "In short: 2 fields, papers and found.");
  await expect(summary).toBeVisible({ timeout: 60_000 });
  await expect(summary.getByText("Survey", { exact: true })).toBeVisible();
  await expect(messageList(page).getByText('{"papers"')).toHaveCount(0);

  // The full output is one click away on the card, pretty-printed, and whole.
  const full = messageList(page).getByTestId("run-card-full-output");
  await expect(full).toHaveCount(1);
  await full.getByText("Full output").click();
  await expect(full.locator("pre")).toHaveText(
    '{\n  "papers": [\n    "A",\n    "B",\n    "C"\n  ],\n  "found": 3\n}',
  );
  const { invocations } = (await (await page.request.get(api(`/chats/${chat.id}/messages`))).json()) as {
    invocations: { status: string; full_output: string | null; summary: string | null; handback_error: string | null }[];
  };
  expect(invocations).toHaveLength(1);
  expect(invocations[0]).toMatchObject({
    status: "succeeded",
    full_output: output,
    summary: "In short: 2 fields, papers and found.",
    handback_error: null,
  });
});

test("a member reads a chat they haven't joined, and posting in it joins them", async ({ page, browser }) => {
  const { api, link, chat } = await setUp(page);
  await page.goto(link(`/chats/${chat.id}`));
  await composer(page).fill("Welcome, all");
  await composer(page).press("Enter");
  await expect(reply(page, "Welcome, all")).toBeVisible();
  const invite = await created<{ token: string }>(page.request, api("/organisation/invites"), {
    expires_in_days: 7,
    role: "participant",
  });

  // Someone else registers and accepts the invitation, in a browser of their own.
  const context = await browser.newContext();
  try {
    const sam = await context.newPage();
    await register(sam, "Sam", "Jones", uniqueSuffix());
    await sam.goto(`/invite/${invite.token}`);
    await sam.getByRole("button", { name: "Accept" }).click();
    await expect(sam).toHaveURL(new RegExp(`^[^?#]*${link("")}(/|$)`));

    // The chat is listed for them, not joined, and they read it without joining.
    await sam.goto(link("/chats"));
    const listed = sam.getByRole("list", { name: "Chats" }).getByRole("link").filter({ hasText: chat.name });
    await expect(listed).toContainText("Not joined");
    await listed.click();
    await expect(messageList(sam)).toContainText("Welcome, all");
    await expect(sam.getByText("Everyone in the organisation can read this chat. Posting in it joins it.")).toBeVisible();
    await expect(sam.getByRole("button", { name: "Join", exact: true })).toBeVisible();
    await expect(sam.getByRole("button", { name: "Leave" })).toHaveCount(0);

    // Posting joins them.
    await composer(sam).fill("Hello, all");
    await composer(sam).press("Enter");
    await expect(reply(sam, "Hello, all")).toBeVisible();
    await expect(sam.getByRole("button", { name: "Leave" })).toBeVisible();
    await expect(sam.getByRole("button", { name: "Join", exact: true })).toHaveCount(0);
    await expect(listed).not.toContainText("Not joined");
    await sam.getByRole("tab", { name: /Members/ }).click();
    const members = sam.getByRole("tabpanel", { name: /Members/ });
    await expect(members).toContainText("Core Loop");
    await expect(members).toContainText("Sam Jones");

    // And the chat's first member sees their message arrive.
    await expect(reply(page, "Hello, all")).toBeVisible({ timeout: 30_000 });
  } finally {
    await context.close();
  }
});
