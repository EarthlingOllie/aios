import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type ChatUser, inOrg } from "./fake-api";

/**
 * The lists have filter buttons and (except Chats) a sort menu, kept in the URL as `?filter=`
 * and `?sort=` and combined with the search.
 */

const list = (page: Page, name: string) => page.getByRole("list", { name });
const links = (page: Page, name: string) => list(page, name).getByRole("link");
const search = (page: Page, name: string) => page.getByRole("searchbox", { name: `Search ${name}` });
const filter = (page: Page, name: string) =>
  page.getByRole("group", { name: "Show" }).getByRole("button", { name, exact: true });
const sort = (page: Page, name: string) => page.getByRole("combobox", { name: `Sort ${name}` });

// How often the chat list asks again (CHAT_LIST_POLL_MS).
const CHAT_LIST_POLL_MS = 5000;
const sam: ChatUser = { id: 2, first_name: "Sam", last_name: "Jones" };

test("the agents can be shown by whether they've been run, and sorted by most runs", async ({ page }) => {
  const api = await fakeApi(page);
  const alpha = api.addAgent("Alpha");
  const beta = api.addAgent("Beta");
  api.addAgent("Gamma");
  api.addAgentRun(alpha);
  for (let n = 0; n < 3; n++) api.addAgentRun(beta);
  await page.goto(inOrg("/agents"));

  await expect(filter(page, "All")).toHaveAttribute("aria-pressed", "true");
  await filter(page, "Used").click();
  await expect(filter(page, "Used")).toHaveAttribute("aria-pressed", "true");
  await expect(filter(page, "All")).toHaveAttribute("aria-pressed", "false");
  await expect(page).toHaveURL(inOrg("/agents?filter=used"));
  await expect(links(page, "Agents")).toHaveText([/Alpha/, /Beta/]);
  await expect(page.getByText("2 of 3")).toBeVisible();

  await filter(page, "Unused").click();
  await expect(page).toHaveURL(inOrg("/agents?filter=unused"));
  await expect(links(page, "Agents")).toHaveText([/Gamma/]);

  // The sort: by name (the server's order, and the default) or by most runs.
  await filter(page, "All").click();
  await expect(page).toHaveURL(inOrg("/agents"));
  await expect(sort(page, "agents")).toHaveValue("name");
  await sort(page, "agents").selectOption({ label: "Most runs" });
  await expect(page).toHaveURL(inOrg("/agents?sort=runs"));
  await expect(links(page, "Agents")).toHaveText([/Beta/, /Alpha/, /Gamma/]);
  // Sorting alone hides nothing.
  await expect(page.getByText(/\d+ of \d+/)).toHaveCount(0);

  // Opening an agent keeps both; Back goes to the previous filter.
  await filter(page, "Used").click();
  await links(page, "Agents").first().click();
  await expect(page).toHaveURL(inOrg(`/agents/${beta.id}?filter=used&sort=runs`));
  await expect(links(page, "Agents")).toHaveText([/Beta/, /Alpha/]);
  await page.goBack();
  await page.goBack();
  await expect(page).toHaveURL(inOrg("/agents?sort=runs"));
  await expect(links(page, "Agents")).toHaveText([/Beta/, /Alpha/, /Gamma/]);
  await expect(filter(page, "All")).toHaveAttribute("aria-pressed", "true");

  // The default sort is left out of the URL.
  await sort(page, "agents").selectOption({ label: "Name" });
  await expect(page).toHaveURL(inOrg("/agents"));
  await expect(links(page, "Agents")).toHaveText([/Alpha/, /Beta/, /Gamma/]);
});

test("filters, sort and search combine, and Clear filters resets them all", async ({ page }) => {
  const api = await fakeApi(page);
  const alpha = api.addAgent("Alpha researcher");
  const beta = api.addAgent("Beta researcher");
  api.addAgent("Gamma researcher");
  api.addAgent("Writer");
  api.addAgentRun(alpha);
  for (let n = 0; n < 2; n++) api.addAgentRun(beta);
  await page.goto(inOrg("/agents?q=research&filter=used&sort=runs"));

  await expect(search(page, "agents")).toHaveValue("research");
  await expect(filter(page, "Used")).toHaveAttribute("aria-pressed", "true");
  await expect(sort(page, "agents")).toHaveValue("runs");
  await expect(links(page, "Agents")).toHaveText([/Beta/, /Alpha/]);
  await expect(page.getByText("2 of 4")).toBeVisible();

  // A filter with nothing left says so.
  await search(page, "agents").fill("gamma");
  await expect(links(page, "Agents")).toHaveCount(0);
  await expect(list(page, "Agents")).toContainText("No agents match “gamma” with these filters.");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).toHaveURL(inOrg("/agents"));
  await expect(search(page, "agents")).toHaveValue("");
  await expect(filter(page, "All")).toHaveAttribute("aria-pressed", "true");
  await expect(sort(page, "agents")).toHaveValue("name");
  await expect(links(page, "Agents")).toHaveCount(4);

  // Back undoes it.
  await page.goBack();
  await expect(page).toHaveURL(inOrg("/agents?q=gamma&filter=used&sort=runs"));
  await expect(search(page, "agents")).toHaveValue("gamma");
  await expect(filter(page, "Used")).toHaveAttribute("aria-pressed", "true");
});

test("choosing a filter straight after typing keeps the search", async ({ page }) => {
  const api = await fakeApi(page);
  api.addAgentRun(api.addAgent("Web researcher"));
  api.addAgent("Web writer");
  api.addAgentRun(api.addAgent("Editor"));
  await page.goto(inOrg("/agents"));

  await search(page, "agents").fill("web");
  await filter(page, "Used").click();
  await expect(page).toHaveURL(inOrg("/agents?q=web&filter=used"));
  await expect(search(page, "agents")).toHaveValue("web");
  await expect(links(page, "Agents")).toHaveText([/Web researcher/]);
});

test("an unknown filter or sort in the URL shows the whole list, in the default order", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addAgent("Alpha");
  api.addAgent("Beta");
  await page.goto(inOrg("/agents?filter=zebra&sort=zebra"));

  await expect(links(page, "Agents")).toHaveText([/Alpha/, /Beta/]);
  await expect(filter(page, "All")).toHaveAttribute("aria-pressed", "true");
  await expect(sort(page, "agents")).toHaveValue("name");
});

test("the workflows can be shown by whether they're empty", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Researcher");
  api.addWorkflow("Nightly report", [{ key: "a", agent }]);
  api.addWorkflow("Draft");
  await page.goto(inOrg("/workflows"));

  await filter(page, "Empty").click();
  await expect(page).toHaveURL(inOrg("/workflows?filter=empty"));
  await expect(links(page, "Workflows")).toHaveText([/Draft/]);
  await expect(sort(page, "workflows")).toHaveValue("name");
});

test("the skills can be sorted", async ({ page }) => {
  const api = await fakeApi(page);
  api.addSkill("Citations").updated_at = "2026-01-01T00:00:00Z";
  api.addSkill("Tone").updated_at = "2026-02-01T00:00:00Z";
  // Sorting by name is the server's order, so the default.
  await page.goto(inOrg("/skills"));

  await expect(links(page, "Skills")).toHaveText([/Citations/, /Tone/]);
  await sort(page, "skills").selectOption({ label: "Recently updated" });
  await expect(page).toHaveURL(inOrg("/skills?sort=updated"));
  await expect(links(page, "Skills")).toHaveText([/Tone/, /Citations/]);
});

test("the skills can be shown by whether an agent uses them, with search and sort", async ({ page }) => {
  const api = await fakeApi(page);
  const citations = api.addSkill("Citations");
  citations.updated_at = "2026-01-01T00:00:00Z";
  const tone = api.addSkill("Tone of voice");
  tone.updated_at = "2026-02-01T00:00:00Z";
  api.addSkill("Tables");
  api.addSkillUse(citations, api.addAgent("Researcher"));
  api.addSkillUse(citations, api.addAgent("Editor"));
  api.addSkillUse(tone, api.addAgent("Writer"));
  await page.goto(inOrg("/skills"));

  // Each item says how many agents use it.
  await expect(links(page, "Skills")).toHaveText([
    /Citations.*Used by 2 agents/,
    /Tone.*Used by 1 agent\b/,
    /Tables/,
  ]);
  await expect(links(page, "Skills").nth(2)).not.toContainText("Used by");

  await filter(page, "Used").click();
  await expect(page).toHaveURL(inOrg("/skills?filter=used"));
  await expect(links(page, "Skills")).toHaveText([/Citations/, /Tone/]);
  await expect(page.getByText("2 of 3")).toBeVisible();

  await filter(page, "Unused").click();
  await expect(page).toHaveURL(inOrg("/skills?filter=unused"));
  await expect(links(page, "Skills")).toHaveText([/Tables/]);

  // With the search and the sort.
  await page.goto(inOrg("/skills?filter=used&sort=updated"));
  await expect(links(page, "Skills")).toHaveText([/Tone/, /Citations/]);
  await search(page, "skills").fill("tone");
  await expect(links(page, "Skills")).toHaveText([/Tone/]);
  await expect(page).toHaveURL(inOrg("/skills?filter=used&sort=updated&q=tone"));
});

test("the chats can be shown by joined, not joined, unread and private, as the list refreshes", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const design = api.addChat("Design");
  api.addChat("Support", { visibility: "public" });
  api.addChat("Town hall", { visibility: "public", owner: sam });
  await page.goto(inOrg("/chats"));

  await expect(sort(page, "chats")).toHaveCount(0);
  await expect(links(page, "Chats")).toHaveCount(3);

  await filter(page, "Joined").click();
  await expect(page).toHaveURL(inOrg("/chats?filter=joined"));
  await expect(links(page, "Chats")).toHaveText([/Support/, /Design/]);

  await filter(page, "Not joined").click();
  await expect(links(page, "Chats")).toHaveText([/Town hall/]);

  await filter(page, "Private").click();
  await expect(links(page, "Chats")).toHaveText([/Design/]);

  await filter(page, "Unread").click();
  await expect(page).toHaveURL(inOrg("/chats?filter=unread"));
  await expect(links(page, "Chats")).toHaveCount(0);
  await expect(list(page, "Chats")).toContainText("No chats match these filters.");

  // Sam posts in Design, and joins us in a new chat: the next refresh shows Design, and the
  // filter still applies to what it brings.
  const lobby = api.addChat("Lobby", { visibility: "public", owner: sam });
  api.addChatMember(lobby, api.me);
  api.addChatMessage(design, "Anyone around?", sam);
  await expect(links(page, "Chats")).toHaveText([/Design/], { timeout: CHAT_LIST_POLL_MS + 2000 });
  await expect(page.getByText("1 of 4")).toBeVisible();
  await expect(filter(page, "Unread")).toHaveAttribute("aria-pressed", "true");
});
