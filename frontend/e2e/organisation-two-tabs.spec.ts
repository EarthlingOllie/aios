import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Two tabs open on two organisations work independently: each page is in the organisation its
 * link names, and everything it asks of the API goes under that organisation's prefix, whatever
 * the other tab opened since. Each tab has its own fake (a fake answers for one organisation),
 * both in one browser, so what the browser remembers is shared as it would be.
 */

const tester = { id: 1, name: "tester", slug: "tester", is_creator: true };
const globex = { id: 2, name: "Globex", slug: "globex", is_creator: false };

const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });

async function post(page: Page, text: string) {
  await composer(page).fill(text);
  await composer(page).press("Enter");
  await expect(messageList(page)).toContainText(text);
}

test("a message posted in each tab lands in that tab's organisation", async ({ context }) => {
  const first = await context.newPage();
  const ours = await fakeApi(first);
  ours.otherMemberships = [globex];
  const design = ours.addChat("Design");

  const second = await context.newPage();
  const theirs = await fakeApi(second);
  theirs.organisation = { ...theirs.organisation, id: globex.id, name: globex.name, slug: globex.slug };
  theirs.otherMemberships = [tester];
  theirs.joinedEarlier = true;
  const sales = theirs.addChat("Sales");

  await first.goto(inOrg(`/chats/${design.id}`));
  await second.goto(`/o/globex/chats/${sales.id}`);
  await post(first, "Hello from tester");
  await post(second, "Hello from Globex");
  // After the second tab opened Globex (and the browser remembered it as the one used last).
  await post(first, "Still tester");

  expect(ours.writes).toEqual([
    ["POST", orgApi(`/chats/${design.id}/messages`), { body: "Hello from tester", mentions: [] }],
    ["POST", orgApi(`/chats/${design.id}/messages`), { body: "Still tester", mentions: [] }],
  ]);
  expect(theirs.writes).toEqual([
    ["POST", orgApi(`/chats/${sales.id}/messages`, globex.id), { body: "Hello from Globex", mentions: [] }],
  ]);
  await expect(first.getByRole("heading", { name: "Design" })).toBeVisible();
  await expect(second.getByRole("heading", { name: "Sales" })).toBeVisible();
});
