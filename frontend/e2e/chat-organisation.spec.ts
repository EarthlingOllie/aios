import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/** Chats are the organisation's: every one is open to its members, names are unique within it, a
 * chat's members add any of its members and agents, and another organisation's chats don't exist
 * here. */

const sam = { id: 2, first_name: "Sam", last_name: "Jones" };

test("the chat header says which member created it", async ({ page }) => {
  const api = await fakeApi(page);
  const theirs = api.addChat("Design", { owner: sam });
  const mine = api.addChat("Planning");
  await page.goto(inOrg(`/chats/${theirs.id}`));

  const header = page.getByRole("heading", { name: "Design", level: 2 }).locator("..");
  await expect(header.getByTestId("provenance")).toHaveText("Created by Sam Jones");

  await page.goto(inOrg(`/chats/${mine.id}`));
  const own = page.getByRole("heading", { name: "Planning", level: 2 }).locator("..");
  await expect(own.getByTestId("provenance")).toHaveText("Created by you");
});

test("another organisation's chats aren't listed, don't open, and don't take a name", async ({ page }) => {
  const api = await fakeApi(page);
  api.addChat("Open", { owner: sam });
  const elsewhere = api.addChat("general", { owner: sam, organisation_id: 99 });
  await page.goto(inOrg("/chats"));

  const list = page.getByRole("list").filter({ hasText: "Open" });
  await expect(list.getByRole("listitem")).toHaveText([/Open.*Not joined/]);

  await page.goto(inOrg(`/chats/${elsewhere.id}`));
  await expect(page.getByText("Not Found")).toBeVisible();

  // "general" is free in this organisation; "Open" isn't.
  await page.goto(inOrg("/chats/new"));
  await page.getByLabel("Name").fill("general");
  await page.getByRole("button", { name: "Create chat" }).click();
  await expect(page.getByRole("heading", { name: "general", level: 2 })).toBeVisible();
  await page.goto(inOrg("/chats/new"));
  await page.getByLabel("Name").fill("Open");
  await page.getByRole("button", { name: "Create chat" }).click();
  await expect(page.getByRole("alert")).toHaveText("The organisation already has a chat with that name.");
});

test("a member adds a fellow member and another member's agent", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: sam, joined_at: "2026-10-15T08:00:00Z", invite_id: null });
  const agent = api.addAgent("Their reviewer");
  agent.created_by = sam;
  const chat = api.addChat("Design");
  await page.goto(inOrg(`/chats/${chat.id}/members`));

  const members = page.getByRole("tabpanel", { name: /Members/ });
  await members.getByLabel("Add a member").selectOption("Sam Jones");
  await members.getByRole("button", { name: "Add member" }).click();
  await expect(members.getByRole("list", { name: "Users" }).getByRole("listitem")).toHaveText([
    /Tester/,
    /Sam Jones/,
  ]);
  // Everyone in the organisation is in it now: nobody left to add.
  await expect(members.getByLabel("Add a member")).toHaveCount(0);

  await members.getByLabel("Add an agent").selectOption("Their reviewer");
  await members.getByRole("button", { name: "Add agent" }).click();
  await expect(members.getByRole("list", { name: "Agents" })).toHaveText("Their reviewer");
  expect(api.writes).toEqual([
    ["POST", orgApi(`/chats/${chat.id}/members`), { kind: "user", id: sam.id }],
    ["POST", orgApi(`/chats/${chat.id}/members`), { kind: "agent", id: agent.id }],
  ]);
});
