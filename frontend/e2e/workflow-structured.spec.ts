import { expect, test, type Locator, type Page } from "@playwright/test";

import { fakeApi, type FakeApi, inOrg, orgApi } from "./fake-api";

const node = (page: Page, key: string) => page.locator(`.react-flow__node[data-id="${key}"]`);
const edge = (page: Page, source: string, target: string) =>
  page.locator(`.react-flow__edge[data-id="${source}->${target}"]`);
const inspector = (page: Page) => page.getByRole("complementary", { name: "Inspector" });

async function choose(page: Page, picker: Locator, name: string) {
  await picker.click();
  await page.getByRole("option", { name, exact: true }).click();
}

const SCORE = { type: "object", properties: { score: { type: "number" } }, required: ["score"] };

function seed(api: FakeApi) {
  const scorer = api.addAgent("Scorer");
  const escalate = api.addAgent("Escalate");
  const triage = api.addWorkflow(
    "Triage",
    [
      { key: "score", agent: scorer },
      { key: "escalate", agent: escalate },
    ],
    [["score", "escalate"]],
  );
  return { scorer, escalate, triage };
}

test("the editor sets an output schema, a template and a condition with the builder", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { triage } = seed(api);
  await page.goto(inOrg(`/workflows/${triage.id}/edit`));

  // The scorer gives structured output.
  await node(page, "score").click();
  await inspector(page).getByText("Structured input and output").click();
  await inspector(page).getByLabel("Output schema").fill(JSON.stringify(SCORE));
  await inspector(page).getByLabel("Output schema").blur();

  // Escalate's input is built from the score.
  await node(page, "escalate").click();
  await inspector(page).getByText("Structured input and output").click();
  await expect(inspector(page).getByText("{{nodes.score}}")).toBeVisible();
  await inspector(page).getByLabel("Input template").fill("Escalate: {{nodes.score.score}}");
  await inspector(page).getByLabel("Input template").blur();

  // The edge is taken only for high scores.
  await edge(page, "score", "escalate").dispatchEvent("click");
  await inspector(page).getByLabel("Field").fill("output.score");
  await inspector(page).getByLabel("Field").blur();
  await inspector(page).getByLabel("Comparison").selectOption({ label: "is more than" });
  await inspector(page).getByLabel("Value").fill("5");
  await inspector(page).getByLabel("Value").blur();

  // And the workflow's own input schema.
  await page.getByLabel("Input schema", { exact: true }).last().fill('{"type": "object"}');
  await page.getByLabel("Input schema", { exact: true }).last().blur();

  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+$/);
  const [method, path, body] = api.writes.at(-1)!;
  expect([method, path]).toEqual(["PUT", orgApi(`/workflows/${triage.id}`)]);
  expect(body).toMatchObject({
    nodes: [
      { key: "score", output_schema: SCORE },
      { key: "escalate", input_template: "Escalate: {{nodes.score.score}}" },
    ],
    edges: [{ source: "score", target: "escalate", condition: { ">": [{ var: "output.score" }, 5] } }],
    input_schema: { type: "object" },
  });
  // The graph labels the conditional edge.
  await expect(page.getByText("if score > 5")).toBeVisible();
});

test("a condition can be written as JSON, and a refused one says why", async ({ page }) => {
  const api = await fakeApi(page);
  const { triage } = seed(api);
  await page.goto(inOrg(`/workflows/${triage.id}/edit`));
  await edge(page, "score", "escalate").dispatchEvent("click");
  await inspector(page).getByRole("button", { name: "Edit as JSON" }).click();
  const raw = inspector(page).getByLabel("Condition (JSONLogic)");
  await raw.fill('{"+": [1, 2]}');
  await raw.blur();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("The operator '+' is not allowed")).toBeVisible();
  await expect(page).toHaveURL(/\/edit$/);

  await raw.fill('{"and": [{"==": [{"var": "input"}, "go"]}, true]}');
  await raw.blur();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+$/);
  // Not a simple comparison, so the label only says it has one.
  await expect(page.getByText("if …")).toBeVisible();
});

test("the start-run form is made from the input schema, and a refusal shows the errors", async ({
  page,
}) => {
  const api = await fakeApi(page);
  const { triage } = seed(api);
  api.addWorkflowVersion(triage, (v) => {
    v.input_schema = {
      type: "object",
      properties: {
        topic: { type: "string", title: "Topic" },
        urgent: { type: "boolean" },
        count: { type: "integer" },
      },
      required: ["topic"],
    };
  });
  await page.goto(inOrg(`/workflows/${triage.id}`));
  await page.getByRole("button", { name: "Run" }).click();
  const form = page.getByRole("form", { name: "Start a run" });
  await expect(form.getByLabel("Topic")).toBeVisible();
  await form.getByLabel("urgent").selectOption("Yes");
  await form.getByLabel("count").fill("3");

  // Topic is required: the browser asks first; past that, the API's errors are shown.
  await form.getByLabel("Topic").evaluate((el: HTMLInputElement) => el.removeAttribute("required"));
  await form.getByRole("button", { name: "Start run" }).click();
  await expect(page.getByText("'topic' is a required property")).toBeVisible();

  await form.getByLabel("Topic").fill("tides");
  await form.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/workflows\/\d+\/runs\/\d+$/);
  const [, , body] = api.writes.at(-1)!;
  expect(body).toEqual({ input_json: { topic: "tides", urgent: true, count: 3 } });
});

test("the run page greys skipped steps and formats structured outputs", async ({ page }) => {
  const api = await fakeApi(page);
  const { triage } = seed(api);
  const run = api.addRun(triage, {
    status: "succeeded",
    finished_at: "2026-10-14T09:00:09Z",
    input_json: { topic: "tides" },
    input: '{"topic":"tides"}',
    output: null,
    output_note: "No final step ran",
    steps: [
      {
        path: ["score"],
        status: "succeeded",
        output: '{"score":2}',
        output_json: { score: 2 },
        conditions: { escalate: false },
        finished_at: "2026-10-14T09:00:05Z",
      },
      { path: ["escalate"], status: "skipped", input: "", finished_at: "2026-10-14T09:00:05Z" },
    ],
  });
  await page.goto(inOrg(`/workflows/${triage.id}/runs/${run.id}`));
  await expect(page.getByText("No final step ran")).toBeVisible();
  await expect(page.getByRole("region", { name: "Input" })).toContainText('"topic": "tides"');
  await expect(node(page, "escalate")).toHaveAttribute("data-state", "skipped");
  await expect(edge(page, "score", "escalate")).toHaveAttribute("data-state", "idle");
  await expect(page.getByRole("listitem").filter({ hasText: "escalate" })).toContainText("Skipped");

  await node(page, "score").click();
  await expect(inspector(page).getByRole("region", { name: "Step output" })).toContainText(
    '"score": 2',
  );
  await expect(inspector(page).getByText("→ escalate not taken")).toBeVisible();
  await node(page, "escalate").click();
  await expect(inspector(page).getByText("Skipped: no active path reached it")).toBeVisible();
});
