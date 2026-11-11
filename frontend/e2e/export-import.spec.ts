import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Exporting an agent or workflow from its page downloads its file; importing one from the
 * Agents or Workflows list first previews what it makes (renamed, or unable to run), then makes
 * it and opens it. Participants are offered neither.
 */

const dialog = (page: import("@playwright/test").Page) =>
  page.getByRole("dialog", { name: "Import an agent or workflow" });
const exported = { name: "poet-v1.json", mimeType: "application/json", buffer: Buffer.from("{}") };

test("an agent's and a workflow's page link to their export", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Poet");
  const workflow = api.addWorkflow("Pipeline", [{ key: "a", agent }]);

  // A download, as a document's is: the browser saves the file under the name the API gives it.
  await page.goto(inOrg(`/agents/${agent.id}`));
  const agentLink = page.getByRole("link", { name: "Export" });
  await expect(agentLink).toHaveAttribute("href", orgApi(`/agents/${agent.id}/export`));
  await expect(agentLink).toHaveAttribute("download");

  await page.goto(inOrg(`/workflows/${workflow.id}`));
  const workflowLink = page.getByRole("link", { name: "Export" });
  await expect(workflowLink).toHaveAttribute("href", orgApi(`/workflows/${workflow.id}/export`));
  await expect(workflowLink).toHaveAttribute("download");
});

test("an import previews what it makes, then makes it and opens it", async ({ page }) => {
  const api = await fakeApi(page);
  api.addAgent("Poet");
  api.nextImport = [
    { kind: "skill", name: "Haiku" },
    {
      kind: "agent",
      name: "Poet",
      imported_name: "Poet (imported)",
      unrunnable: "This version can't run: Unknown tool: crm_connector",
    },
  ];
  await page.goto(inOrg("/agents"));

  await page.getByRole("button", { name: "Import" }).click();
  await dialog(page).getByLabel("Exported file").setInputFiles(exported);

  const preview = dialog(page).getByRole("region", { name: "Import preview" });
  await expect(preview.getByRole("listitem")).toHaveText([
    "Skill Haiku",
    /Agent Poet \(imported\) · renamed from Poet: that name is taken\s*This version can't run: Unknown tool: crm_connector/,
  ]);
  // The preview was a dry run: nothing made yet.
  expect(api.imports).toEqual([{ filename: "poet-v1.json", dryRun: true }]);
  await expect(page.getByRole("list", { name: "Agents" }).getByRole("link")).toHaveCount(1);

  await dialog(page).getByRole("button", { name: "Import" }).click();

  await expect(dialog(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Poet \(imported\)/ })).toBeVisible();
  await expect(page.getByRole("note").filter({ hasText: "can't run" })).toContainText(
    "This version can't run: Unknown tool: crm_connector",
  );
  await expect(page.getByRole("list", { name: "Agents" }).getByRole("link")).toHaveCount(2);
  expect(api.imports.at(-1)).toEqual({ filename: "poet-v1.json", dryRun: false });
});

test("a file that isn't an export is refused in the preview, and Import stays off", async ({ page }) => {
  await fakeApi(page);
  await page.goto(inOrg("/workflows"));

  await page.getByRole("button", { name: "Import" }).click();
  await dialog(page).getByLabel("Exported file").setInputFiles(exported);

  await expect(dialog(page).getByRole("alert")).toContainText("That file isn't an export");
  await expect(dialog(page).getByRole("button", { name: "Import" })).toBeDisabled();
});

test("a participant is offered neither export nor import", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  const agent = api.addAgent("Poet");
  await page.goto(inOrg(`/agents/${agent.id}`));

  await expect(page.getByRole("heading", { name: /Poet/ })).toBeVisible();
  await expect(page.getByRole("link", { name: "Export" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Import" })).toHaveCount(0);
});
