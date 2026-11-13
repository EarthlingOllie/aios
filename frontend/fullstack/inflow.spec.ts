import { expect, test, type Page } from "@playwright/test";

import { created, mention, messageList, reply, setUp } from "./stack";

/**
 * The inflow loop against the real stack, every sync and turn on the run service: upstream, the
 * test provider's channel has a message and a web page is up (changed through the stack's testing
 * routes, `aios.testing.feeds_api`, as the provider and the web would change them); an admin
 * connects the test connector, picks the channel and adds a URL source for the page, and each
 * syncs; the organisation assistant, on the scripted model's `core-loop` scenario
 * (backend/src/aios/testing/scenarios.py), searches and answers from the synced message, citing
 * it; the message is deleted upstream, the next sync removes it, and the assistant finds nothing
 * to answer from; an agent given the connector's `post_message` posts to the channel (seen
 * upstream), and the audit log records the action; Insights shows the turns and the sources'
 * health. Every way a document comes in gives it a source: an upload (Uploads), the web page
 * (its URL source) and the removed connector's content, kept as documents in Uploads.
 *
 * Each link of the loop is a step that waits for what it should have done (a sync's items, the
 * assistant's reply, the post upstream, the audit entry, the Insights rows), so breaking one
 * fails the test there.
 */

const ASSISTANT = "Organisation assistant";
const ANSWER = "Zebrafish embryos are transparent. [Zebrafish]";
// The scripted assistant's words when its search finds nothing synced (scenarios.NOTHING_FOUND).
const NOTHING_FOUND = "Nothing I found answers that.";

const connectorRow = (page: Page, title: string) =>
  page.getByRole("list", { name: "Connectors" }).getByRole("listitem").filter({ hasText: title });
const scopeOption = (page: Page, name: string) =>
  page.getByRole("list", { name: "Scope options" }).getByRole("listitem").filter({ hasText: name });
const documentRow = (page: Page, filename: string) =>
  page.getByRole("list", { name: "Documents" }).getByRole("listitem").filter({ hasText: filename });

/** Changes something upstream through the stack's testing routes. */
async function upstream(page: Page, method: "put" | "delete", path: string, data?: object) {
  const response = await page.request[method](`/api/testing${path}`, data ? { data } : {});
  expect(response.status(), await response.text()).toBe(204);
}

type SourceOut = { id: number; name: string; item_count: number; syncs: { status: string; removed: number }[] };
type DocumentOut = { filename: string; source: { id: number; name: string } };

test("inflow: a connector and a web page sync, the assistant answers from them, a removal and a tool action follow", async ({
  page,
}) => {
  // The whole loop: four syncs, four turns.
  test.setTimeout(300_000);
  const { api, link, chat, suffix } = await setUp(page);
  const channel = `inflow-${suffix}`;
  const pagePath = `${channel}/prices.md`;
  const pageFile = `test.invalid-pages-${channel}-prices.md`;
  const source = async (id: number) => (await (await page.request.get(api(`/sources/${id}`))).json()) as SourceOut;
  const documents = async () => (await (await page.request.get(api("/documents"))).json()) as DocumentOut[];

  // Upstream: a message in the channel, and a web page.
  await upstream(page, "put", `/feeds/${channel}/messages/zebrafish`, {
    text: "Zebrafish embryos are transparent.",
    title: "Zebrafish",
  });
  await upstream(page, "put", `/pages/${pagePath}`, { text: "# Prices\n\nWidgets cost four pounds." });

  // The admin connects the test connector (its authorisation page sends the browser straight
  // back) and picks the channel; its first sync brings the message in.
  await page.goto(link("/data/sources"));
  await connectorRow(page, "Test").getByRole("button", { name: "Connect" }).click();
  await expect(page).toHaveURL(/\/data\/connectors\/\d+$/);
  const connectorId = Number(new URL(page.url()).pathname.split("/").pop());
  await page.getByRole("textbox", { name: "Search what it can sync" }).fill(channel);
  await scopeOption(page, channel).getByRole("checkbox").check();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("region", { name: "Syncing" })).toBeVisible();
  const connected = (await (await page.request.get(api(`/connectors/${connectorId}`))).json()) as {
    source: { id: number; name: string };
  };
  const synced = connected.source;
  await expect
    .poll(async () => (await source(synced.id)).item_count, { message: "the connector's first sync", timeout: 60_000 })
    .toBe(1);

  // A URL source for the page; its first sync makes the page a document of the source.
  await page.goto(link("/data/sources"));
  await page.getByRole("button", { name: "Add source" }).click();
  const form = page.getByRole("form", { name: "Add source" });
  await expect(form.getByLabel("Kind")).toHaveValue("url");
  await form.getByLabel("Name").fill("Price page");
  await form.getByLabel("URL").fill(`https://test.invalid/pages/${pagePath}`);
  await form.getByRole("button", { name: "Add" }).click();
  await expect(form).toBeHidden();
  await expect
    .poll(async () => (await documents()).find((d) => d.filename === pageFile)?.source.name, {
      message: "the web page's first sync",
      timeout: 60_000,
    })
    .toBe("Price page");

  // An upload, for the Documents tab to show beside it.
  await page.goto(link("/data/documents"));
  await page.locator('input[type="file"]').setInputFiles({
    name: `notes-${suffix}.md`,
    mimeType: "text/markdown",
    buffer: Buffer.from("# Notes\n\nThe tank needs cleaning."),
  });
  await expect(documentRow(page, `notes-${suffix}.md`).getByTitle("Source")).toHaveText("Uploads");
  await page.reload();
  await expect(documentRow(page, pageFile).getByTitle("Source")).toHaveText("Price page");
  await expect(documentRow(page, pageFile)).toContainText("Synced");

  // The organisation assistant (a default chat agent: it's in every new chat) answers from the
  // synced message, citing it.
  await page.goto(link(`/chats/${chat.id}`));
  await mention(page, "what about zebrafish?", ASSISTANT);
  const answer = reply(page, ANSWER);
  await expect(answer).toBeVisible({ timeout: 60_000 });
  await expect(answer.getByText(ASSISTANT, { exact: true })).toBeVisible();

  // Deleted upstream, the message is removed by the next sync (Sync now, on its source's page).
  await upstream(page, "delete", `/feeds/${channel}/items/zebrafish`);
  await page.goto(link(`/data/sources/${synced.id}`));
  await page.getByRole("button", { name: "Sync now" }).click();
  await expect
    .poll(async () => (await source(synced.id)).item_count, { message: "the removing sync", timeout: 60_000 })
    .toBe(0);
  await page.reload();
  await expect(page.getByRole("list", { name: "Syncs" }).getByRole("listitem").first()).toContainText("1 removed");

  // Asked again, the assistant finds nothing synced to answer from.
  await page.goto(link(`/chats/${chat.id}`));
  await mention(page, "what about zebrafish?", ASSISTANT);
  await expect(reply(page, NOTHING_FOUND)).toBeVisible({ timeout: 60_000 });
  await expect(reply(page, ANSWER)).toHaveCount(1);

  // An agent given the connector's post_message posts to the channel.
  const poster = await created<{ id: number }>(page.request, api("/agents"), {
    name: "Poster",
    base_prompt: `Post to channel: ${channel}`,
    tools: [
      { key: "connector", config: { connectors: [{ connector_id: connectorId, provider: "test", tools: ["post_message"] }] } },
    ],
  });
  await created(page.request, api(`/chats/${chat.id}/members`), { kind: "agent", id: poster.id });
  await page.reload();
  await mention(page, "the tank is clean", "Poster");
  await expect(reply(page, "Posted.")).toBeVisible({ timeout: 60_000 });
  await expect(messageList(page)).not.toContainText("Couldn't post");
  // Upstream, the channel has the post.
  const feed = (await (await page.request.get(`/api/testing/feeds/${channel}`)).json()) as {
    items: { text: string }[];
  };
  // (The composer puts a space after the mention, as well as the one typed.)
  expect(feed.items.map((i) => i.text.replace(/\s+/g, " "))).toEqual(["@Poster the tank is clean"]);

  // The audit log records the action, the agent's, without its content.
  await expect
    .poll(async () => {
      const audit = await page.request.get(api("/audit-log?action=connector.tool_called"));
      const { entries } = (await audit.json()) as {
        entries: { actor: { kind: string; agent: { name: string } | null }; details: Record<string, unknown> }[];
      };
      return entries.map((e) => ({
        by: e.actor.agent?.name,
        tool: e.details.tool,
        target: e.details.target,
        outcome: e.details.outcome,
        content: JSON.stringify(e).includes("tank is clean"),
      }));
    })
    .toEqual([{ by: "Poster", tool: "post_message", target: channel, outcome: "succeeded", content: false }]);
  await page.goto(link("/organisation/audit-log"));
  await expect(page.getByText("post_message").first()).toBeVisible();

  // Insights shows the turns (the assistant's two, the poster's one) and the sources' health.
  await page.goto(link("/insights"));
  const usage = page.getByRole("region", { name: "Usage and cost", exact: true });
  await expect(usage.getByRole("row", { name: new RegExp(ASSISTANT) }).getByRole("cell").nth(1)).toHaveText("2");
  await expect(usage.getByRole("row", { name: /Poster/ }).getByRole("cell").nth(1)).toHaveText("1");
  const health = page.getByRole("region", { name: "Source health", exact: true });
  await expect(health.getByRole("row", { name: /Price page/ })).toContainText("Healthy");
  await expect(health.getByRole("row", { name: new RegExp(synced.name) })).toBeVisible();

  // The post syncs in; removing the connector, keeping what it synced, makes it a document in
  // Uploads.
  await page.goto(link(`/data/sources/${synced.id}`));
  await page.getByRole("button", { name: "Sync now" }).click();
  await expect
    .poll(async () => (await source(synced.id)).item_count, { message: "the post synced in", timeout: 60_000 })
    .toBe(1);
  await page.goto(link(`/data/connectors/${connectorId}`));
  await page.getByRole("button", { name: "Remove connector" }).click();
  const remove = page.getByRole("form", { name: "Remove connector" });
  await remove.getByLabel("Keep it as documents in Uploads").check();
  await remove.getByRole("button", { name: "Remove" }).click();
  await expect
    .poll(async () => (await documents()).filter((d) => d.filename.startsWith(`${channel} `)).map((d) => d.source.name), {
      message: "the connector's content kept as documents",
      timeout: 60_000,
    })
    .toEqual(["Uploads"]);

  // Every document has its source: the upload's and the kept content's are Uploads, the web
  // page's its URL source.
  await page.goto(link("/data/documents"));
  const kept = (await documents()).find((d) => d.filename.startsWith(`${channel} `))!.filename;
  await expect(documentRow(page, kept).getByTitle("Source")).toHaveText("Uploads");
  await expect(documentRow(page, `notes-${suffix}.md`).getByTitle("Source")).toHaveText("Uploads");
  await expect(documentRow(page, pageFile).getByTitle("Source")).toHaveText("Price page");
});
