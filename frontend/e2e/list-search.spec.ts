import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type ChatUser } from "./fake-api";

/**
 * The lists on the left of each section have a search box over each item's name and
 * description. The search is kept in the URL (`?q=`), and carried as you open items.
 */

const list = (page: Page, name: string) => page.getByRole("list", { name });
const links = (page: Page, name: string) => list(page, name).getByRole("link");
const search = (page: Page, name: string) => page.getByRole("searchbox", { name: `Search ${name}` });

// How often the chat list asks again (CHAT_LIST_POLL_MS).
const CHAT_LIST_POLL_MS = 5000;
const sam: ChatUser = { id: 2, login: "sam", name: "Sam" };

test("typing narrows the agents by name or description, and shows how many are left", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addAgent("Researcher", 1, "Finds sources on the web");
  api.addAgent("Writer", 1, "Drafts the café newsletter");
  api.addAgent("Editor", 1, "Checks drafts");
  await page.goto("/agents");

  await expect(links(page, "Agents")).toHaveCount(3);
  await expect(page.getByText(/\d+ of \d+/)).toHaveCount(0);

  // Every word has to match, in either field, ignoring case and accents.
  await search(page, "agents").fill("DRAFTS cafe");
  await expect(links(page, "Agents")).toHaveText([/Writer/]);
  await expect(page.getByText("1 of 3")).toBeVisible();

  await search(page, "agents").fill("drafts");
  await expect(links(page, "Agents")).toHaveText([/Writer/, /Editor/]);
  await expect(page.getByText("2 of 3")).toBeVisible();

  // The clear button empties it.
  await page.getByRole("button", { name: "Clear search" }).click();
  await expect(search(page, "agents")).toHaveValue("");
  await expect(links(page, "Agents")).toHaveCount(3);
  await expect(page.getByText(/\d+ of \d+/)).toHaveCount(0);
  await expect(page).toHaveURL(/\/agents$/);

  // So does Escape.
  await search(page, "agents").fill("web");
  await expect(links(page, "Agents")).toHaveCount(1);
  await search(page, "agents").press("Escape");
  await expect(search(page, "agents")).toHaveValue("");
  await expect(links(page, "Agents")).toHaveCount(3);
});

test("a search nothing matches says so, and Clear filters brings the list back", async ({ page }) => {
  const api = await fakeApi(page);
  api.addAgent("Researcher");
  await page.goto("/agents");

  await search(page, "agents").fill("zebra");
  await expect(links(page, "Agents")).toHaveCount(0);
  await expect(list(page, "Agents")).toContainText("No agents match “zebra”.");
  await expect(page.getByText("No agents yet")).toHaveCount(0);

  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(search(page, "agents")).toHaveValue("");
  await expect(links(page, "Agents")).toHaveText([/Researcher/]);
});

test("an empty list still says it's empty", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/agents");
  await expect(page.getByText("No agents yet")).toBeVisible();
  await expect(page.getByRole("button", { name: "Clear filters" })).toHaveCount(0);
});

test("the search is in the URL: applied on load, updated as you type, and restored by Back", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const alpha = api.addAgent("Alpha");
  api.addAgent("Beta");
  await page.goto("/agents?q=alp");

  await expect(search(page, "agents")).toHaveValue("alp");
  await expect(links(page, "Agents")).toHaveText([/Alpha/]);

  // Opening an item keeps the search.
  await links(page, "Agents").first().click();
  await expect(page).toHaveURL(`/agents/${alpha.id}?q=alp`);
  await expect(links(page, "Agents")).toHaveText([/Alpha/]);

  // Typing replaces the history entry rather than adding one per key.
  await search(page, "agents").clear();
  await search(page, "agents").pressSequentially("beta");
  await expect(page).toHaveURL(`/agents/${alpha.id}?q=beta`);
  await expect(links(page, "Agents")).toHaveText([/Beta/]);
  // The open agent stays open, though the search hides it from the list.
  await expect(page.getByRole("heading", { name: "Alpha" })).toBeVisible();

  // So Back goes to the previous search, not the previous key press.
  await page.goBack();
  await expect(page).toHaveURL("/agents?q=alp");
  await expect(search(page, "agents")).toHaveValue("alp");
  await expect(links(page, "Agents")).toHaveText([/Alpha/]);

  // A reload keeps it.
  await page.reload();
  await expect(search(page, "agents")).toHaveValue("alp");
  await expect(links(page, "Agents")).toHaveText([/Alpha/]);
});

test("New and deleting an agent keep the search; another agent opens on its first tab", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const first = api.addAgent("Researcher one");
  const second = api.addAgent("Researcher two");
  api.addAgent("Writer");
  await page.goto(`/agents/${first.id}/runs?q=research`);

  // Another item's link carries the search, but not the open tab.
  await expect(list(page, "Agents").getByRole("link", { name: /Researcher two/ })).toHaveAttribute(
    "href",
    `/agents/${second.id}?q=research`,
  );
  await expect(page.getByRole("link", { name: "New" })).toHaveAttribute("href", "/agents/new?q=research");
  await page.getByRole("link", { name: "New" }).click();
  await expect(page).toHaveURL("/agents/new?q=research");
  await expect(links(page, "Agents")).toHaveCount(2);

  await list(page, "Agents").getByRole("link", { name: /Researcher one/ }).click();
  await page.getByRole("button", { name: "Delete agent" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete agent" }).click();
  await expect(page).toHaveURL("/agents?q=research");
  await expect(links(page, "Agents")).toHaveText([/Researcher two/]);
  await expect(search(page, "agents")).toHaveValue("research");
});

test("the workflows list can be searched", async ({ page }) => {
  const api = await fakeApi(page);
  api.addWorkflow("Nightly report");
  api.addWorkflow("Onboarding").description = "Welcomes a new hire";
  await page.goto("/workflows");

  await search(page, "workflows").fill("hire");
  await expect(links(page, "Workflows")).toHaveText([/Onboarding/]);
  await expect(page.getByText("1 of 2")).toBeVisible();
  await expect(page).toHaveURL("/workflows?q=hire");
});

test("the skills list can be searched", async ({ page }) => {
  const api = await fakeApi(page);
  api.addSkill("Citations", 1, "Cite every source");
  api.addSkill("Tone", 1, "Write plainly");
  await page.goto("/skills");

  await search(page, "skills").fill("source");
  await expect(links(page, "Skills")).toHaveText([/Citations/]);
  await expect(page.getByText("1 of 2")).toBeVisible();
});

test("creating a chat from a searched list keeps the search", async ({ page }) => {
  const api = await fakeApi(page);
  api.addChat("Design review");
  api.addChat("Support");
  await page.goto("/chats?q=design");

  await page.getByRole("link", { name: "New", exact: true }).click();
  await page.getByLabel("Name").fill("Design system");
  await page.getByRole("button", { name: "Create chat" }).click();

  await expect(page).toHaveURL(/\/chats\/\d+\?q=design$/);
  const created = [...api.chats.values()].find((c) => c.name === "Design system")!;
  await expect(page).toHaveURL(`/chats/${created.id}?q=design`);
  await expect(links(page, "Chats")).toHaveCount(2);
  await expect(search(page, "chats")).toHaveValue("design");
});

test("the chats list keeps the search as it refreshes", async ({ page }) => {
  const api = await fakeApi(page);
  api.addChat("Design");
  api.addChat("Support", { description: "Customer questions" });
  await page.goto("/chats?q=customer");

  await expect(links(page, "Chats")).toHaveText([/Support/]);

  // Chats that arrive with the next refresh are searched too.
  api.addChat("Customer feedback", { visibility: "public", owner: sam });
  api.addChat("Random", { visibility: "public", owner: sam });
  await expect(links(page, "Chats")).toHaveCount(2, { timeout: CHAT_LIST_POLL_MS + 2000 });
  await expect(links(page, "Chats").filter({ hasText: "Random" })).toHaveCount(0);
  await expect(search(page, "chats")).toHaveValue("customer");
  await expect(page.getByText("2 of 4")).toBeVisible();
});
