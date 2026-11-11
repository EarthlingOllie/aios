import { expect, test } from "@playwright/test";

import { fakeApi, inOrg, orgApi } from "./fake-api";

/**
 * Roles: what a participant sees (edit controls hidden or disabled, saying why), and an admin
 * changing roles on the Members tab and choosing the role an invitation link grants.
 */

const BUILD_REFUSAL =
  "Participants can't create, edit, archive or unarchive agents, skills, workflows and schedules. Ask an editor or an admin.";
const DOCUMENTS_REFUSAL = "Participants can't upload or delete documents. Ask an editor or an admin.";
const SAM = { id: 2, first_name: "Sam", last_name: "Jones" };

test("a participant can run an agent but not change it, and is told why", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  const agent = api.addAgent("Triage");
  api.addSchedule("agent", agent);
  await page.goto(inOrg(`/agents/${agent.id}`));

  await expect(page.getByRole("heading", { name: /Triage/ })).toBeVisible();
  await expect(page.getByRole("note", { name: "What your role allows" }).first()).toHaveText(BUILD_REFUSAL);
  await expect(page.getByRole("button", { name: "New run" })).toBeEnabled();
  await expect(page.getByRole("link", { name: "Edit", exact: true })).toBeHidden();
  await expect(page.getByRole("button", { name: "Archive", exact: true })).toBeHidden();
  const create = page.getByRole("button", { name: "New", exact: true });
  await expect(create).toBeDisabled();
  await expect(create).toHaveAttribute("title", BUILD_REFUSAL);

  await page.goto(inOrg(`/agents/${agent.id}/schedule`));
  await expect(page.getByRole("button", { name: "Add schedule" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Edit schedule" })).toBeHidden();
  await expect(page.getByRole("button", { name: "Delete schedule" })).toBeHidden();
  await expect(page.getByRole("checkbox", { name: "Pause schedule" })).toBeDisabled();
});

test("a participant's skills and workflows have no edit controls", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  const skill = api.addSkill("Tone");
  const workflow = api.addWorkflow("Pipeline", [{ key: "a", agent: api.addAgent("Writer") }]);

  await page.goto(inOrg(`/skills/${skill.id}`));
  await expect(page.getByRole("note", { name: "What your role allows" })).toHaveText(BUILD_REFUSAL);
  await expect(page.getByRole("link", { name: "Edit", exact: true })).toBeHidden();
  await expect(page.getByRole("button", { name: "Archive", exact: true })).toBeHidden();

  await page.goto(inOrg(`/workflows/${workflow.id}`));
  await expect(page.getByRole("note", { name: "What your role allows" })).toHaveText(BUILD_REFUSAL);
  await expect(page.getByRole("button", { name: "Run" })).toBeEnabled();
  await expect(page.getByRole("link", { name: "Edit", exact: true })).toBeHidden();
  await expect(page.getByRole("button", { name: "Archive", exact: true })).toBeHidden();

  // Opened directly, the editor says why it can't save.
  await page.goto(inOrg("/agents/new"));
  await expect(page.getByRole("note", { name: "What your role allows" })).toHaveText(BUILD_REFUSAL);
  await expect(page.getByRole("button", { name: "Create agent" })).toBeDisabled();
});

test("a participant downloads documents but can't upload or delete them", async ({ page }) => {
  const api = await fakeApi(page);
  api.actAs("participant");
  api.addDocument("pricing.md");
  await page.goto(inOrg("/data/documents"));

  await expect(page.getByRole("note", { name: "What your role allows" })).toHaveText(DOCUMENTS_REFUSAL);
  await expect(page.getByRole("link", { name: "Download pricing.md" })).toBeAttached();
  await expect(page.getByRole("button", { name: "Delete pricing.md" })).toHaveCount(0);
  const upload = page.getByRole("button", { name: "Upload" });
  await expect(upload).toBeDisabled();
  await expect(upload).toHaveAttribute("title", DOCUMENTS_REFUSAL);
});

test("a participant sees the members' roles but can't change them or invite", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: SAM, joined_at: "2026-10-15T09:00:00Z", invite_id: null });
  api.actAs("participant");
  await page.goto(inOrg("/organisation"));

  await expect(page.getByRole("button", { name: "Rename" })).toBeHidden();
  await expect(page.getByRole("tab", { name: "Invitations" })).toBeHidden();
  await expect(page.getByLabel("Terry Tester's role", { exact: true })).toHaveText("Participant");
  await expect(page.getByRole("combobox", { name: "Sam Jones's role" })).toBeHidden();
  await expect(page.getByRole("note", { name: "What your role allows" })).toContainText(
    "Participants can't change the organisation's roles",
  );

  await page.getByRole("tab", { name: "Settings" }).click();
  const rules = page.getByRole("region", { name: "Approval rules" });
  await expect(rules).toContainText("Drop a column");
  await expect(rules.getByRole("combobox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete organisation" })).toBeHidden();
});

test("an admin changes a member's role on the Members tab", async ({ page }) => {
  const api = await fakeApi(page);
  api.organisation.members.push({ user: SAM, joined_at: "2026-10-15T09:00:00Z", invite_id: null });
  await page.goto(inOrg("/organisation"));

  const sams = page.getByRole("combobox", { name: "Sam Jones's role" });
  await expect(sams).toHaveValue("editor");
  await sams.selectOption("participant");
  await expect(sams).toHaveValue("participant");
  expect(api.writes).toEqual([["PUT", orgApi("/organisation/members/2/role"), { role: "participant" }]]);

  // The last admin can't step down: the API's refusal is shown, and nothing changes.
  const mine = page.getByRole("combobox", { name: "Terry Tester's role" });
  await mine.selectOption("editor");
  await expect(page.getByText("An organisation needs an admin")).toBeVisible();
  await expect(mine).toHaveValue("admin");
});

test("an admin chooses the role an invitation link grants", async ({ page }) => {
  const api = await fakeApi(page);
  await page.goto(inOrg("/organisation/invitations"));
  await page.getByRole("combobox", { name: "Joins as" }).selectOption("participant");
  await page.getByRole("button", { name: "Make a link" }).click();

  await expect(page.getByRole("textbox", { name: "New link" })).toBeVisible();
  expect(api.writes).toEqual([
    ["POST", orgApi("/organisation/invites"), { expires_in_days: 7, role: "participant" }],
  ]);
  await expect(page.getByRole("list", { name: "Open links" })).toContainText("joins as participant");
});
