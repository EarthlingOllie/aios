import { expect, test } from "@playwright/test";

import { fakeApi, inOrg } from "./fake-api";

/** Agents and workflows are the organisation's: each says which member made it and who last
 * changed it, on its list and its page. */

const sam = { id: 2, first_name: "Sam", last_name: "Jones" };

test("an agent says which member made it and who last changed it", async ({ page }) => {
  const api = await fakeApi(page);
  const agent = api.addAgent("Reviewer");
  agent.created_by = sam;
  await page.goto(inOrg(`/agents/${agent.id}`));

  const list = page.getByRole("list", { name: "Agents" });
  await expect(list.getByTestId("provenance")).toHaveText(
    "Created by Sam Jones · last changed by you",
  );
  const header = page.getByRole("heading", { name: /Reviewer/ }).locator("..");
  await expect(header.getByTestId("provenance")).toHaveText(
    "Created by Sam Jones · last changed by you",
  );
});

test("a workflow made and changed by one member names only them", async ({ page }) => {
  const api = await fakeApi(page);
  const workflow = api.addWorkflow("Pipeline");
  workflow.created_by = sam;
  workflow.updated_by = sam;
  await page.goto(inOrg(`/workflows/${workflow.id}`));

  const list = page.getByRole("list", { name: "Workflows" });
  await expect(list.getByTestId("provenance")).toHaveText("Created by Sam Jones");
});
