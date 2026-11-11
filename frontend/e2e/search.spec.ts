import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, type FakeApi } from "./fake-api";

/** The Search page: one search over documents, chat messages, past runs and briefs, each result
 * labelled with its kind and linked to its source, a filter by kind, and the ranking explanation
 * behind "Show ranking". */

const results = (page: Page) => page.getByRole("region", { name: "Results" });
const kindsGroup = (page: Page) => page.getByRole("group", { name: "Kinds" });

function everything(api: FakeApi) {
  api.addDocument("otters.md");
  api.searchItems.push(
    {
      id: 1,
      kind: "chat_message",
      text: "The otters escaped again.",
      created_at: "2026-10-15T09:00:00Z",
      ref: {},
      chat_id: 7,
      chat_name: "zoo",
      message_id: 70,
    },
    {
      id: 2,
      kind: "run_message",
      text: "Otters hold hands while they sleep.",
      created_at: "2026-10-15T09:01:00Z",
      ref: { role: "assistant" },
      agent_id: 3,
      agent_name: "Keeper",
      agent_run_id: 30,
      run_title: "About otters",
    },
    {
      id: 3,
      kind: "brief",
      text: "The team is tracking the otters.",
      created_at: "2026-10-15T09:02:00Z",
      ref: {},
      agent_id: 3,
      agent_name: "Keeper",
      chat_id: 7,
      chat_name: "zoo",
    },
  );
}

test("one search finds every kind, each labelled and linked to its source", async ({ page }) => {
  const api = await fakeApi(page);
  everything(api);
  await page.goto(inOrg("/search?q=otter"));

  const found = results(page);
  await expect(found).toContainText("4 results, best match first");
  await expect(found.getByRole("article", { name: "Document" })).toContainText("otters.md");
  const message = found.getByRole("article", { name: "Chat message" });
  await expect(message).toContainText("The otters escaped again.");
  await expect(message.getByRole("link", { name: "#zoo" })).toHaveAttribute("href", inOrg("/chats/7"));
  const run = found.getByRole("article", { name: "Run" });
  await expect(run.getByRole("link", { name: "Keeper · About otters" })).toHaveAttribute(
    "href",
    inOrg("/agents/3/runs/30"),
  );
  await expect(run).toContainText("Replied");
  await expect(found.getByRole("article", { name: "Brief" })).toContainText("Keeper in #zoo");

  await message.getByRole("link", { name: "#zoo" }).click();
  await expect(page).toHaveURL(/\/chats\/7$/);
});

test("the kind filter narrows the search and stays in the URL", async ({ page }) => {
  const api = await fakeApi(page);
  everything(api);
  await page.goto(inOrg("/search?q=otter"));
  await expect(kindsGroup(page).getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
  await expect(results(page).getByRole("article")).toHaveCount(4);

  await kindsGroup(page).getByRole("button", { name: "Chat messages" }).click();
  await expect(page).toHaveURL(/[?&]kind=chat_message/);
  await expect(kindsGroup(page).getByRole("button", { name: "Chat messages" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(results(page).getByRole("article")).toHaveCount(1);
  await expect(results(page).getByRole("article", { name: "Chat message" })).toBeVisible();
  expect(api.searches.at(-1)).toContain("kind=chat_message");

  await kindsGroup(page).getByRole("button", { name: "Documents" }).click();
  await expect(results(page).getByRole("article")).toHaveCount(1);
  await expect(results(page).getByRole("article", { name: "Document" })).toBeVisible();

  // Back to everything: the request names no kind.
  await kindsGroup(page).getByRole("button", { name: "All" }).click();
  await expect(results(page).getByRole("article")).toHaveCount(4);
  expect(api.searches.at(-1)).not.toContain("kind=");

  // Linked to, the filter comes with it.
  await page.goto(inOrg("/search?q=otter&kind=brief"));
  await expect(results(page).getByRole("article")).toHaveCount(1);
  await expect(results(page).getByRole("article", { name: "Brief" })).toBeVisible();
});

test("the ranking explanation is behind the Show ranking toggle", async ({ page }) => {
  const api = await fakeApi(page);
  everything(api);
  await page.goto(inOrg("/search?q=otter"));
  const first = results(page).getByRole("article").first();
  await expect(first).toBeVisible();
  await expect(page.getByText(/Semantic #/)).toHaveCount(0);
  await expect(page.getByText(/Keyword #/)).toHaveCount(0);
  const searched = api.searches.length;

  const toggle = page.getByRole("checkbox", { name: "Show ranking" });
  await toggle.click();
  await expect(toggle).toBeChecked();
  await expect(first.getByText("Semantic #1")).toBeVisible();
  await expect(first.getByText("Keyword #1")).toBeVisible();
  await expect(first.getByText("0.0300")).toBeVisible();
  await expect(page).toHaveURL(/[?&]ranking=1/);
  // Showing it searches nothing again.
  expect(api.searches.length).toBe(searched);

  await toggle.click();
  await expect(toggle).not.toBeChecked();
  await expect(page.getByText(/Semantic #/)).toHaveCount(0);
});

test("the run history export's panel is gone from the organisation's settings", async ({ page }) => {
  await fakeApi(page);
  const asked: string[] = [];
  page.on("request", (r) => asked.push(new URL(r.url()).pathname));
  await page.goto(inOrg("/organisation/settings"));
  await expect(page.getByRole("region", { name: "Approval rules" })).toBeVisible();
  await expect(page.getByText("Run history export")).toHaveCount(0);
  // Nothing asks for the export's settings any more.
  expect(asked.filter((path) => path.startsWith("/api/") && path.includes("/settings"))).toEqual([]);
});
