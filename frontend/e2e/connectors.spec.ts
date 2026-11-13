import { expect, test, type Page } from "@playwright/test";

import { fakeApi, inOrg, RESTRICTED_WARNING } from "./fake-api";

/**
 * Connectors on the Sources tab: the providers this deployment offers, and why any aren't;
 * connecting (admins only) and landing on the connector's page; picking what it syncs, with the
 * warning to accept for anything restricted at its source; Reconnect when its access failed; and
 * removing it, keeping or deleting what it synced.
 */

const connectorList = (page: Page) =>
  page.getByRole("list", { name: "Connectors" });
const providerRow = (page: Page, title: string) =>
  connectorList(page).getByRole("listitem").filter({ hasText: title });
const option = (page: Page, name: string) =>
  page
    .getByRole("list", { name: "Scope options" })
    .getByRole("listitem")
    .filter({ hasText: name });

test("the providers list says which can be connected, and why not", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto(inOrg("/data/sources"));

  await expect(providerRow(page, "Slack")).toContainText(
    "needs its app settings",
  );
  await expect(
    providerRow(page, "Slack").getByRole("button", { name: "Connect" }),
  ).toBeDisabled();
  await expect(
    providerRow(page, "Test").getByRole("button", { name: "Connect" }),
  ).toBeEnabled();
});

test("an admin connects, picks what syncs, accepting the warning for something restricted", async ({
  page,
}) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/data/sources"));

  await providerRow(page, "Test")
    .getByRole("button", { name: "Connect" })
    .click();

  await expect(page).toHaveURL(/\/data\/connectors\/1$/);
  await expect(page.getByRole("heading", { name: /Test/ })).toContainText(
    "Connected",
  );
  await expect(option(page, "board")).toContainText("Restricted");

  // Searching narrows the options.
  await page
    .getByRole("textbox", { name: "Search what it can sync" })
    .fill("gen");
  await expect(option(page, "random")).toBeHidden();
  await option(page, "general").getByRole("checkbox").check();
  await page.getByRole("textbox", { name: "Search what it can sync" }).fill("");
  await option(page, "board").getByRole("checkbox").check();

  const warning = page
    .getByRole("alert")
    .filter({ hasText: "restricted at its source" });
  await expect(warning).toHaveText(
    new RegExp(RESTRICTED_WARNING.replace(/[.?]/g, "\\$&")),
  );
  const save = page.getByRole("button", { name: "Save" });
  await expect(save).toBeDisabled();
  await warning.getByRole("checkbox", { name: "I understand" }).check();
  await save.click();

  await expect
    .poll(() => api.scopeSaves)
    .toEqual([{ id: 1, items: ["general", "board"], acknowledged: true }]);
  await expect(page.getByRole("region", { name: "Syncing" })).toBeVisible();
  await page.goto(inOrg("/data/sources"));
  await expect(providerRow(page, "Test")).toContainText("Syncs general, board");
});

test("picking only unrestricted items needs no acknowledgement", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addConnector("test");
  await page.goto(inOrg("/data/connectors/1"));

  await option(page, "random").getByRole("checkbox").check();
  await expect(page.getByRole("alert")).toBeHidden();
  await page.getByRole("button", { name: "Save" }).click();

  await expect
    .poll(() => api.scopeSaves)
    .toEqual([{ id: 1, items: ["random"], acknowledged: false }]);
});

test("a connector whose access failed shows Reconnect, and reconnecting resumes it", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const source = api.addSource("Test", "connector", {
    status: "needs_reauth",
    health: "needs_reconnecting",
  });
  api.addConnector("test", {
    status: "needs_reauth",
    source_id: source.id,
    scope: [{ id: "general", name: "general", restricted: false }],
  });
  await page.goto(inOrg("/data/sources"));
  await expect(providerRow(page, "Test")).toContainText("Reconnect");

  await providerRow(page, "Test").getByRole("link", { name: "Test" }).click();
  await expect(
    page.getByRole("note").filter({ hasText: "stopped syncing" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Reconnect" }).click();

  await expect(page.getByRole("heading", { name: /Test/ })).toContainText(
    "Connected",
  );
  await expect(page.getByRole("button", { name: "Reconnect" })).toBeHidden();
});

for (const keep of [true, false]) {
  test(`removing a connector ${keep ? "keeps" : "deletes"} what it synced, as the admin chooses`, async ({
    page,
  }) => {
    const api = await fakeApi(page);
    api.addConnector("test");
    await page.goto(inOrg("/data/connectors/1"));

    await page.getByRole("button", { name: "Remove connector" }).click();
    const form = page.getByRole("form", { name: "Remove connector" });
    await form
      .getByRole("radio", {
        name: keep ? "Keep it as documents in Uploads" : "Delete it",
      })
      .check();
    await form.getByRole("button", { name: "Remove" }).click();

    await expect(page).toHaveURL(/\/data\/sources$/);
    expect(api.connectorRemovals).toEqual([{ id: 1, keep }]);
    await expect(
      providerRow(page, "Test").getByRole("button", { name: "Connect" }),
    ).toBeEnabled();
  });
}

for (const role of ["editor", "participant"] as const) {
  test(`${role === "editor" ? "an editor" : "a participant"} sees connectors, with the actions disabled`, async ({
    page,
  }) => {
    const api = await fakeApi(page);
    api.actAs(role);
    api.addConnector("google_drive", {
      scope: [{ id: "general", name: "general", restricted: false }],
    });
    await page.goto(inOrg("/data/sources"));

    const connect = providerRow(page, "Test").getByRole("button", {
      name: "Connect",
    });
    await expect(connect).toBeDisabled();
    await expect(connect).toHaveAttribute("title", /Ask an admin/);

    await providerRow(page, "Google Drive")
      .getByRole("link", { name: "Google Drive" })
      .click();
    await expect(
      page.getByRole("note", { name: "What your role allows" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Remove connector" }),
    ).toBeDisabled();
    await expect(option(page, "general").getByRole("checkbox")).toBeDisabled();
  });
}

test("a connector whose provider was switched off says so", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.addConnector("test", {
    unavailable: "Test is switched off in this deployment.",
  });
  await page.goto(inOrg("/data/connectors/1"));

  await expect(page.getByRole("heading", { name: /Test/ })).toContainText(
    "Unavailable",
  );
  await expect(page.getByText("what it synced stays searchable")).toBeVisible();
});

test("an admin turns a GitHub connector's README and docs syncing on, and its health shows on the Data page", async ({
  page,
}) => {
  const api = await fakeApi(page);
  api.scopeOptions = [
    { id: "acme/api", name: "acme/api", restricted: false },
    { id: "acme/secret-plans", name: "acme/secret-plans", restricted: true },
  ];
  const source = api.addSource("GitHub", "connector", {
    provider: "github",
    status: "failing",
    health: "failing",
  });
  const github = api.addConnector("github", {
    scope: [{ id: "acme/api", name: "acme/api", restricted: false }],
    source_id: source.id,
  });
  await page.goto(inOrg(`/data/connectors/${github.id}`));

  const docs = page.getByRole("checkbox", {
    name: /Also sync README and docs files/,
  });
  await expect(docs).not.toBeChecked();
  await docs.check();
  await page.getByRole("button", { name: "Save" }).click();

  await expect
    .poll(() => api.scopeSaves)
    .toEqual([
      { id: github.id, items: ["acme/api"], acknowledged: false, docs: true },
    ]);
  await expect(docs).toBeChecked();

  await page.goto(inOrg("/data/sources"));
  await expect(
    page
      .getByRole("list", { name: "Sources" })
      .getByRole("listitem")
      .filter({ hasText: "GitHub" })
      .getByLabel("Health: Failing"),
  ).toBeVisible();
});

test("connectors without documentation files don't offer docs syncing", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const connector = api.addConnector("test");
  await page.goto(inOrg(`/data/connectors/${connector.id}`));

  await expect(page.getByRole("region", { name: "What it syncs" })).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /Also sync README and docs files/ }),
  ).toHaveCount(0);
});
