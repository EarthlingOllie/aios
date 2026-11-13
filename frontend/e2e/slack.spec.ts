import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/**
 * Slack: the scope picker says private channels must invite the app first; a channel archived or
 * left by the app is marked as needing attention, on its page and on the Data page, where the
 * connector's health shows; and an editor gives an agent post_to_slack limited to channels they
 * pick.
 */

const HINT =
  "Only channels the aios app can see are listed: every public channel, and private channels it has been invited to. To sync a private channel, invite the app to it in Slack first (/invite @aios), then search again.";
const LEFT =
  "The aios app is no longer in this channel, so its messages were removed. Invite it back in Slack to sync it again.";

test("the scope picker says private channels must invite the app first", async ({ page }) => {
  const api = await fakeApi(page);
  const slack = api.addConnector("slack", { scope_hint: HINT });
  await page.goto(inOrg(`/data/connectors/${slack.id}`));

  await expect(page.getByRole("region", { name: "What it syncs" }).getByRole("note")).toHaveText(HINT);
});

test("a channel left by the app needs attention, and the connector's health shows on the Data page", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const source = api.addSource("Slack", "connector", { provider: "slack" });
  const slack = api.addConnector("slack", {
    scope: [
      { id: "C0GENERAL", name: "#general", restricted: false },
      { id: "G0PLANS", name: "#plans", restricted: true },
    ],
    attention: [{ id: "C0GENERAL", name: "#general", reason: LEFT }],
    source_id: source.id,
  });
  await page.goto(inOrg(`/data/connectors/${slack.id}`));

  const attention = page.getByRole("region", { name: "Needs attention" });
  await expect(attention).toContainText("#general");
  await expect(attention).toContainText(LEFT);

  await page.goto(inOrg("/data/sources"));
  const row = page.getByRole("list", { name: "Connectors" }).getByRole("listitem").filter({ hasText: "Slack" });
  await expect(row).toContainText("Needs attention: #general");
  await expect(row.getByLabel("Health: Healthy")).toBeVisible();
});

test("an editor gives an agent post_to_slack, limited to the channels they pick", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  api.addTool(
    "connector",
    "Connector tools",
    { properties: { connectors: { type: "array", title: "Connectors", "x-picker": "connectors" } } },
    { connectors: [] },
  );
  const slack = api.addConnector("slack");
  const herald = api.addAgent("Herald");
  await page.goto(inOrg(`/agents/${herald.id}/edit`));

  await page.getByRole("checkbox", { name: /Connector tools/ }).check();
  await page.getByRole("checkbox", { name: /post_to_slack/ }).check();
  const allowed = page.getByRole("group", { name: "post_to_slack: allowed channels" });
  await expect(allowed).toContainText("Pick at least one channel.");
  await allowed.getByRole("checkbox", { name: "#general" }).check();
  await expect(allowed).not.toContainText("Pick at least one channel.");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page).toHaveURL(inOrg(`/agents/${herald.id}`));
  const [, , body] = api.writes.at(-1)!;
  expect(body).toMatchObject({
    tools: [
      {
        key: "connector",
        config: {
          connectors: [
            {
              connector_id: slack.id,
              provider: "slack",
              tools: ["post_to_slack"],
              allowed: { post_to_slack: ["C0GENERAL"] },
            },
          ],
        },
      },
    ],
  });
});
