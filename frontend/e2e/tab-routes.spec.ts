import { expect, test, type Page } from "@playwright/test";

import { fakeApi, type FakeApi, inOrg } from "./fake-api";

/**
 * Every tabbed page keeps its open tab in the route: the first tab is the page's own path and
 * the others a segment under it (`/workflows/3/runs`). The route decides the tab on load, a
 * click moves to the tab's route, and non-canonical URLs land on the canonical one.
 */

type TabbedPage = {
  name: string;
  /** Makes what the page shows, and gives its own path. */
  seed: (api: FakeApi) => string;
  /** The tabs' ids, in order, and the name each tab's button goes by. */
  tabs: [id: string, label: RegExp][];
};

const pages: TabbedPage[] = [
  {
    name: "agent",
    seed: (api) => `/agents/${api.addAgent("Researcher").id}`,
    tabs: [
      ["context", /Context/],
      ["runs", /Runs/],
      ["versions", /Versions/],
      ["schedule", /Schedule/],
    ],
  },
  {
    name: "workflow",
    seed: (api) => `/workflows/${api.addWorkflow("Report").id}`,
    tabs: [
      ["graph", /Graph/],
      ["runs", /Runs/],
      ["versions", /Versions/],
      ["schedule", /Schedule/],
    ],
  },
  {
    name: "skill",
    seed: (api) => `/skills/${api.addSkill("Release notes").id}`,
    tabs: [
      ["content", /Content/],
      ["used-by", /Used by/],
      ["versions", /Versions/],
    ],
  },
  {
    name: "chat",
    seed: (api) => `/chats/${api.addChat("Design").id}`,
    tabs: [
      ["chat", /^Chat$/],
      ["members", /Members/],
    ],
  },
  {
    name: "Data",
    seed: () => "/data",
    tabs: [
      ["overview", /Overview/],
      ["tables", /Tables/],
      ["changes", /Changes/],
      ["dropped", /Dropped/],
      ["documents", /Documents/],
    ],
  },
];

// The page's own tabs, not those inside a panel (the chat composer's Write and Preview).
const tablist = (page: Page) => page.getByRole("tablist").first();
const tab = (page: Page, name: RegExp) => tablist(page).getByRole("tab", { name });
const selected = (page: Page) => tablist(page).getByRole("tab", { selected: true });
const href = (path: string, id: string, first: string) => (id === first ? path : `${path}/${id}`);

for (const { name, seed, tabs } of pages) {
  const [first] = tabs[0];

  test(`the ${name} page opens each tab from its route, and a click moves to the tab's route`, async ({
    page,
  }) => {
    const path = inOrg(seed(await fakeApi(page)));

    // Each tab's own URL opens on it, also after a reload.
    for (const [id, label] of tabs) {
      await page.goto(href(path, id, first));
      await expect(selected(page)).toHaveText(label);
      await page.reload();
      await expect(selected(page)).toHaveText(label);
      await expect(page).toHaveURL(href(path, id, first));
    }

    // Clicking walks the tabs' routes, last to first, keeping the query.
    await page.goto(`${href(path, tabs.at(-1)![0], first)}?q=x`);
    for (const [id, label] of [...tabs].reverse()) {
      await tab(page, label).click();
      await expect(page).toHaveURL(`${href(path, id, first)}?q=x`);
      await expect(selected(page)).toHaveText(label);
      await expect(page.getByRole("tabpanel")).toBeVisible();
    }
  });

  test(`the ${name} page sends non-canonical tab URLs to the canonical one`, async ({ page }) => {
    const path = inOrg(seed(await fakeApi(page)));
    const [second, secondLabel] = tabs[1];

    await page.goto(`${path}/${first}`);
    await expect(page).toHaveURL(path);
    await expect(selected(page)).toHaveText(tabs[0][1]);

    await page.goto(`${path}/no-such-tab`);
    await expect(page).toHaveURL(path);
    await expect(selected(page)).toHaveText(tabs[0][1]);

    // An old ?tab= link still works, landing on the tab's route.
    await page.goto(`${path}?tab=${second}&q=x`);
    await expect(page).toHaveURL(`${path}/${second}?q=x`);
    await expect(selected(page)).toHaveText(secondLabel);
  });
}

test("another item opens on its first tab, and Back leaves the page rather than its tabs", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const one = api.addAgent("Researcher");
  const two = api.addAgent("Writer");
  await page.goto(inOrg("/agents"));
  await page.getByRole("list", { name: "Agents" }).getByRole("link", { name: /Researcher/ }).click();

  await tab(page, /Runs/).click();
  await tab(page, /Versions/).click();
  await expect(page).toHaveURL(inOrg(`/agents/${one.id}/versions`));

  await page.getByRole("list", { name: "Agents" }).getByRole("link", { name: /Writer/ }).click();
  await expect(page).toHaveURL(inOrg(`/agents/${two.id}`));
  await expect(selected(page)).toHaveText(/Context/);

  await page.goBack();
  await expect(page).toHaveURL(inOrg(`/agents/${one.id}/versions`));
  await page.goBack();
  await expect(page).toHaveURL(inOrg("/agents"));
});
