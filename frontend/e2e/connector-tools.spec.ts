import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** Editors give agents connectors' tools: each connector is a group of the tools it offers in the
 * agent editor's tool picker, and the version pins the connector and the tool names picked. A
 * turn that left tools out says so on the run's page. */

const CONNECTOR = {
  properties: {
    connectors: {
      type: "array",
      title: "Connectors",
      description: "Each connector's tools the agent may use.",
      "x-picker": "connectors",
    },
  },
};

test("an editor gives an agent a connector's tools, picked by name", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  api.addTool("connector", "Connector tools", CONNECTOR, { connectors: [] });
  const connector = api.addConnector("test");
  const poster = api.addAgent("Poster");
  await page.goto(inOrg(`/agents/${poster.id}/edit`));

  await page.getByRole("checkbox", { name: /Connector tools/ }).check();
  const group = page.getByTestId("connector-group");
  await expect(group).toContainText(connector.name);
  await expect(group.getByRole("checkbox")).toHaveCount(2);
  await expect(group).toContainText("read-only");
  await group.getByRole("checkbox", { name: /post_message/ }).check();
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page).toHaveURL(inOrg(`/agents/${poster.id}`));
  const [, , body] = api.writes.at(-1)!;
  expect(body).toMatchObject({
    tools: [
      {
        key: "connector",
        config: { connectors: [{ connector_id: connector.id, provider: "test", tools: ["post_message"] }] },
      },
    ],
  });
});

test("a pinned tool the connector no longer offers says so and can be taken off", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  api.addTool("connector", "Connector tools", CONNECTOR, { connectors: [] });
  const connector = api.addConnector("test");
  const poster = api.addAgent("Poster");
  poster.toolSettings = {
    connector: { connectors: [{ connector_id: connector.id, provider: "test", tools: ["post_message", "launch"] }] },
  };
  await page.goto(inOrg(`/agents/${poster.id}/edit`));

  const gone = page.getByRole("checkbox", { name: /launch/ });
  await expect(gone).toBeChecked();
  await expect(page.getByTestId("connector-group")).toContainText("no longer offered");
  // Taken off, it's gone: the connector doesn't offer it to pick again.
  await gone.click();
  await expect(gone).toHaveCount(0);
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page).toHaveURL(inOrg(`/agents/${poster.id}`));
  const [, , body] = api.writes.at(-1)!;
  expect(body).toMatchObject({
    tools: [
      {
        key: "connector",
        config: { connectors: [{ connector_id: connector.id, provider: "test", tools: ["post_message"] }] },
      },
    ],
  });
});

test("a connector that needs reconnecting says why its tools can't be picked", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  api.addTool("connector", "Connector tools", CONNECTOR, { connectors: [] });
  api.addConnector("test", { status: "needs_reauth" });
  const poster = api.addAgent("Poster");
  await page.goto(inOrg(`/agents/${poster.id}/edit`));

  await page.getByRole("checkbox", { name: /Connector tools/ }).check();

  await expect(page.getByTestId("connector-group")).toContainText("This connector needs reconnecting first.");
});

test("a turn that left tools out says so with the run's turns", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Poster");
  const run = api.addAgentRun(agent, "Post it", 2);
  const notice = "The Test connector no longer offers the tool read_channel, so it was left out.";
  api.addTurn(run, { notices: [notice] });
  await page.goto(inOrg(`/agents/${agent.id}/runs/${run.id}`));

  await page.getByRole("tab", { name: "Turns" }).click();

  await expect(page.getByTestId("turn-notices")).toHaveText(notice);
});

test("an editor limits a GitHub connector's tools to allowed repositories", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("editor");
  api.addTool("connector", "Connector tools", CONNECTOR, { connectors: [] });
  const github = api.addConnector("github");
  const reporter = api.addAgent("Reporter");
  await page.goto(inOrg(`/agents/${reporter.id}/edit`));

  await page.getByRole("checkbox", { name: /Connector tools/ }).check();
  const group = page.getByTestId("connector-group");
  await expect(group.getByRole("textbox", { name: "Allowed repositories" })).toHaveCount(0);
  await group.getByRole("checkbox", { name: /comment_on_issue/ }).check();
  await group.getByRole("checkbox", { name: /open_issue/ }).check();
  const allowed = group.getByRole("textbox", { name: "Allowed repositories" });
  await expect(group).toContainText("with none, they can't act at all");
  await allowed.fill("acme/api, acme/website");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page).toHaveURL(inOrg(`/agents/${reporter.id}`));
  const [, , body] = api.writes.at(-1)!;
  expect(body).toMatchObject({
    tools: [
      {
        key: "connector",
        config: {
          connectors: [
            {
              connector_id: github.id,
              provider: "github",
              tools: ["comment_on_issue", "open_issue"],
              targets: ["acme/api", "acme/website"],
            },
          ],
        },
      },
    ],
  });
});
