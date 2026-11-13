import { expect, type APIRequestContext, type Page } from "@playwright/test";

/**
 * What the full-stack stories share: registering people, an organisation with its store
 * provisioned, an agent in a chat, and mentioning in the composer. Each story is a scenario of the
 * scripted model (backend/src/aios/testing/scenarios.py).
 */

export const messageList = (page: Page) => page.getByRole("log", { name: "Messages" });
export const composer = (page: Page) => page.getByRole("textbox", { name: "Message", exact: true });
export const cardStatus = (page: Page) => messageList(page).getByTestId("run-card-status");
/** The chat's message (not a card) with this text in it. */
export const reply = (page: Page, text: string | RegExp) =>
  messageList(page).getByRole("article").filter({ hasText: text });

export const NOTES_TOOLS = [
  { key: "request_schema_change", config: {} },
  { key: "write_data", config: {} },
];

/** A run of digits telling one story's people and organisations from another's. */
export const uniqueSuffix = () => `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;

export async function created<T>(request: APIRequestContext, url: string, data: object): Promise<T> {
  const response = await request.post(url, { data });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()) as T;
}

/** Registers a new person, which makes nothing: they're left on the welcome page. */
export async function register(page: Page, first: string, last: string, suffix: string) {
  await page.goto("/register");
  await page.getByLabel("First name").fill(first);
  await page.getByLabel("Last name").fill(last);
  await page.getByLabel("Email").fill(`${first}-${last}-${suffix}@example.com`.toLowerCase());
  await page.getByLabel("Password", { exact: true }).fill("a long passphrase");
  await page.getByLabel("Confirm password").fill("a long passphrase");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/welcome$/);
  await expect(page.getByRole("heading", { name: "Welcome to aios" })).toBeVisible();
}

export type Organisation = {
  /** Its API path: the API names it by its id (`/api/o/<id>/...`). */
  api: (path: string) => string;
  /** Its page path: links name it by its slug. */
  link: (path: string) => string;
};

/** The organisation the signed-in person is in (their first). */
export async function organisationOf(page: Page): Promise<Organisation> {
  const me = await page.request.get("/api/me");
  expect(me.status(), await me.text()).toBe(200);
  const {
    organisations: [organisation],
  } = (await me.json()) as { organisations: { id: number; slug: string }[] };
  return {
    api: (path: string) => `/api/o/${organisation.id}${path}`,
    link: (path: string) => `/o/${organisation.slug}${path}`,
  };
}

/** Registers a new person ("Core Loop"), who creates an organisation (its store provisioned by
 * the routine worker), an agent that may ask for tables and write to them, and a chat it's in. */
export async function setUp(page: Page) {
  const suffix = uniqueSuffix();
  await register(page, "Core", "Loop", suffix);
  // Create an organisation on the welcome page: its store is provisioned by the routine worker.
  await page.getByLabel("Organisation name").fill(`Core Loop ${suffix}`);
  await page.getByRole("button", { name: "Create organisation" }).click();
  await expect(page).toHaveURL(/\/o\/[^/]+\//);
  const { api, link } = await organisationOf(page);
  await expect(page).toHaveURL(new RegExp(`^[^?#]*${link("")}(/|$)`));
  await expect
    .poll(
      async () => {
        const response = await page.request.get(api("/data-store"));
        return response.ok() ? (await response.json()).status : `HTTP ${response.status()}`;
      },
      { message: "the organisation's data store is provisioned", timeout: 60_000 },
    )
    .toBe("ready");

  // An agent that may ask for tables and write to them, in a chat (their screens have tests of
  // their own).
  const agent = await created<{ id: number }>(page.request, api("/agents"), { name: "Notes", tools: NOTES_TOOLS });
  const chat = await created<{ id: number; name: string }>(page.request, api("/chats"), { name: `Notes ${suffix}` });
  await created(page.request, api(`/chats/${chat.id}/members`), { kind: "agent", id: agent.id });
  return { api, link, agent, chat, suffix };
}

/** Mentions the agent or workflow (the Notes agent unless `name` and `kind` say) in the open
 * chat, with `text` after the mention. */
export async function mention(page: Page, text: string, name = "Notes", kind: "agent" | "workflow" = "agent") {
  await composer(page).pressSequentially(`@${name.slice(0, 3)}`);
  await page.getByRole("listbox", { name: "Mention" }).getByRole("option", { name: `${name} ${kind}` }).click();
  await composer(page).pressSequentially(` ${text}`);
  await composer(page).press("Enter");
}
