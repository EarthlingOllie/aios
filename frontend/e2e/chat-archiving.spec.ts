import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Chats without owners: the screens follow what the API says each person may do to a chat
 * (`actions`). Admins archive and unarchive a chat; an archived chat is read-only, with a banner,
 * and listed under the Archived filter. Nobody deletes a chat.
 */

const links = (page: Page) => page.getByRole("list", { name: "Chats" }).getByRole("link");
const show = (page: Page, name: string) =>
  page.getByRole("group", { name: "Show" }).getByRole("button", { name, exact: true });
const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const sam = { id: 2, first_name: "Sam", last_name: "Jones" };

test("an admin archives a chat, which turns read-only and moves to the Archived filter, and unarchives it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addAgent("Scout");
  const chat = api.addChat("Design");
  api.addChat("Other");
  api.addChatMessage(chat, "Notes for the team");
  await page.goto(inOrg(`/chats/${chat.id}`));

  await expect(composer(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Archive", exact: true }).click();

  await expect(page.getByRole("status", { name: "Archived" })).toContainText(
    "This chat was archived by you. It's read-only",
  );
  await expect(composer(page)).toHaveCount(0);
  await expect(page.getByRole("status", { name: "Read-only" })).toBeVisible();
  // Still there to read.
  await expect(page.getByRole("log", { name: "Messages" })).toContainText("Notes for the team");
  await expect(page.getByRole("link", { name: "Edit" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Archive", exact: true })).toHaveCount(0);
  // The default list leaves it out; the Archived filter shows it.
  await expect(links(page)).toHaveText([/Other/]);
  await show(page, "Archived").click();
  await expect(page).toHaveURL(inOrg(`/chats/${chat.id}?filter=archived`));
  await expect(links(page)).toHaveText([/Design.*archived/]);
  // Its members' agents can't be added or removed while it's archived.
  await page.getByRole("tab", { name: /Members/ }).click();
  const members = page.getByRole("tabpanel", { name: /Members/ });
  await expect(members.getByLabel("Add an agent")).toHaveCount(0);

  await page.getByRole("button", { name: "Unarchive" }).click();
  await expect(page.getByRole("status", { name: "Archived" })).toHaveCount(0);
  await expect(members.getByLabel("Add an agent")).toBeVisible();
  await expect(links(page)).toHaveCount(0);
  await show(page, "All").click();
  await expect(links(page)).toHaveText([/Other/, /Design/]);
  expect(api.writes).toEqual([
    ["POST", orgApi(`/chats/${chat.id}/archive`), null],
    ["POST", orgApi(`/chats/${chat.id}/unarchive`), null],
  ]);
});

test("an editor can't archive, and sees an archived chat read-only with nothing to join", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: sam, joined_at: "2026-10-15T08:00:00Z", invite_id: null, role: "admin" });
  api.actAs("editor");
  const open = api.addChat("Open", { owner: sam });
  const old = api.addChat("Old", { owner: sam });
  Object.assign(old, { archived_at: "2026-11-10T09:00:00Z", archived_by: sam });

  await page.goto(inOrg(`/chats/${open.id}`));
  await expect(page.getByRole("button", { name: "Join", exact: true })).toBeVisible();
  await expect(composer(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Archive", exact: true })).toHaveCount(0);

  await page.goto(inOrg(`/chats/${old.id}?filter=archived`));
  await expect(page.getByRole("status", { name: "Archived" })).toContainText("This chat was archived by Sam Jones.");
  await expect(page.getByRole("button", { name: "Join", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Unarchive" })).toHaveCount(0);
  await expect(composer(page)).toHaveCount(0);
  expect(api.writes).toEqual([]);
});
