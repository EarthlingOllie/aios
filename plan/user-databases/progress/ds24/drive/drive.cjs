// Drives the real SPA (vite, proxied to the real API on the ds24 cluster) for #24: a new user's
// store is provisioned; they upload a document, which the real ingest worker writes into their
// store; the search page, the documents chat and an agent with the document search tool find
// it. A second user sees none of it.
// Usage: node drive.cjs <base url> <out dir>
const path = require("node:path");
const { chromium } = require(
  path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"),
);

const [base, out] = process.argv.slice(2);
const shot = (page, name) => page.screenshot({ path: path.join(out, name), fullPage: true });

async function signIn(browser, githubId) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on("response", (r) => {
    if (r.url().includes("/api/"))
      console.log(`  [${githubId}] ${r.request().method()} ${r.url().replace(base, "")} ${r.status()}`);
  });
  await page.goto(`${base}/auth/callback?code=${githubId}&state=y`);
  await page.waitForURL(`${base}/`);
  // Wait for the store to be provisioned (the real provisioning worker): Upload is enabled.
  const upload = page.getByRole("button", { name: "Upload" });
  const deadline = Date.now() + 120000;
  while (!(await upload.isEnabled())) {
    if (Date.now() > deadline) throw new Error("the store never became ready");
    await page.waitForTimeout(250);
  }
  return page;
}

async function chat(page, text) {
  const box = page.getByRole("textbox", { name: "Message" });
  await box.fill(text);
  await box.press("Enter");
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });

  console.log("1. user A signs up; their store is provisioned");
  const a = await signIn(browser, -2400);
  await shot(a, "1-a-ready.png");

  console.log("2. user A uploads; the ingest worker writes the chunks into A's store");
  await a.locator('input[type="file"]').setInputFiles({
    name: "hedgehogs.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Hedgehogs\n\nHedgehogs eat beetles and worms at night.\n\nThey hibernate in winter.",
    ),
  });
  await a.getByText(/3 chunks/).waitFor({ timeout: 60000 });
  console.log("   hedgehogs.md: ready, 3 chunks");
  await shot(a, "2-a-uploaded.png");

  console.log("3. the search page finds the passage");
  await a.goto(`${base}/search?q=what+do+hedgehogs+eat`);
  await a.getByText("beetles", { exact: false }).first().waitFor({ timeout: 20000 });
  await shot(a, "3-a-search.png");

  console.log("4. the documents chat searches A's store");
  await a.goto(`${base}/`);
  await chat(a, "what do hedgehogs eat");
  await a.getByText(/Found: .*beetles/).first().waitFor({ timeout: 30000 });
  console.log("   reply:", (await a.getByText(/Found: /).first().innerText()).slice(0, 120));
  await shot(a, "4-a-documents-chat.png");

  console.log("5. an agent with the document search tool searches A's store");
  const created = await a.request.post(`${base}/api/agents`, {
    data: { name: "Librarian", base_prompt: "", skill_version_ids: [] },
  });
  const agent = await created.json();
  const v = agent.current_version;
  const put = await a.request.put(`${base}/api/agents/${agent.id}`, {
    data: {
      name: agent.name,
      description: agent.description,
      base_prompt: v.base_prompt,
      skill_version_ids: [],
      tools: [{ key: "hybrid_search", config: { max_results: 3 } }],
    },
  });
  console.log("   agent saved with hybrid_search:", put.status());
  const run = await (await a.request.post(`${base}/api/agents/${agent.id}/runs`)).json();
  await a.goto(`${base}/agents/${agent.id}/runs/${run.id}`);
  await chat(a, "hedgehogs in winter");
  await a.getByText(/Found: .*hibernate/).first().waitFor({ timeout: 30000 });
  console.log("   reply:", (await a.getByText(/Found: /).first().innerText()).slice(0, 120));
  await shot(a, "5-a-agent-run.png");

  console.log("6. user B signs up: none of A's documents, search and chat find nothing");
  const b = await signIn(browser, -2401);
  const docs = await (await b.request.get(`${base}/api/documents`)).json();
  console.log("   B's documents:", JSON.stringify(docs));
  if (docs.length !== 0) throw new Error("B sees documents");
  await shot(b, "6-b-no-documents.png");
  await b.goto(`${base}/search?q=what+do+hedgehogs+eat`);
  await b.waitForTimeout(2000);
  const hits = await (await b.request.get(`${base}/api/search?q=hedgehogs`)).json();
  console.log("   B's search hits:", JSON.stringify(hits));
  if (hits.length !== 0) throw new Error("B's search found A's documents");
  await shot(b, "7-b-search.png");
  await b.goto(`${base}/`);
  await chat(b, "what do hedgehogs eat");
  await b.getByText(/Found: /).first().waitFor({ timeout: 30000 });
  const reply = await b.getByText(/Found: /).first().innerText();
  console.log("   B's chat reply:", reply);
  if (reply.includes("beetles")) throw new Error("B's chat found A's documents");
  await shot(b, "8-b-documents-chat.png");

  console.log("DONE");
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
