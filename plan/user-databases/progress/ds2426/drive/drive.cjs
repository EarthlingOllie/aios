// Drives the real SPA (vite, proxied to the real API on the ds2426 cluster) for the #24 + #26
// merge: a new user's store is provisioned (real worker); they upload a document, which the
// real ingest worker writes into their store; one agent with the document search tool AND both
// data tools searches it, then lists the store's documents with describe_data and a query_data
// that reads aios.documents.
// Usage: node drive.cjs <base url> <out dir>
const path = require("node:path");
const { chromium } = require(
  path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"),
);

const [base, out] = process.argv.slice(2);
const shot = (page, name) => page.screenshot({ path: path.join(out, name), fullPage: true });

async function ask(page, text, expect, name) {
  await page.goto(page.url());
  const box = page.getByRole("textbox", { name: "Message" });
  await box.fill(text);
  await box.press("Enter");
  const answer = page.getByText(expect).last();
  await answer.waitFor({ timeout: 30000 });
  console.log(`asked ${JSON.stringify(text)} -> ${JSON.stringify(await answer.innerText())}`);
  for (const summary of await page.locator("details > summary").all()) {
    const details = summary.locator("..");
    if (!(await details.evaluate((d) => d.open))) await summary.click();
  }
  await shot(page, name);
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  page.on("response", (r) => {
    if (r.url().includes("/api/"))
      console.log(`  ${r.request().method()} ${r.url().replace(base, "")} ${r.status()}`);
  });

  console.log("1. sign up; the store is provisioned by the real worker");
  await page.goto(`${base}/auth/callback?code=-2426&state=y`);
  await page.waitForURL(`${base}/`);
  const upload = page.getByRole("button", { name: "Upload" });
  const deadline = Date.now() + 120000;
  while (!(await upload.isEnabled())) {
    if (Date.now() > deadline) throw new Error("the store never became ready");
    await page.waitForTimeout(250);
  }
  const store = await (await page.request.get(`${base}/api/data-store`)).json();
  console.log("   data store:", JSON.stringify(store));
  await shot(page, "1-ready.png");

  console.log("2. upload; the ingest worker writes it into the store");
  await page.locator('input[type="file"]').setInputFiles({
    name: "hedgehogs.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Hedgehogs\n\nHedgehogs eat beetles and worms at night.\n\nThey hibernate in winter.",
    ),
  });
  await page.getByText(/3 chunks/).waitFor({ timeout: 60000 });
  await shot(page, "2-uploaded.png");

  console.log("3. an agent with Document search, Describe data and Query data");
  await page.goto(`${base}/agents/new`);
  await page.locator("#agent-name").fill("Librarian analyst");
  await page.getByLabel("Document search").check();
  await page.getByLabel("Describe data").check();
  await page.getByLabel("Query data").check();
  await shot(page, "3-agent-form.png");
  await page.getByRole("button", { name: "Create agent" }).click();
  await page.getByRole("button", { name: "New run" }).waitFor();
  await shot(page, "4-agent-detail.png");
  await page.getByRole("button", { name: "New run" }).click();
  await page.waitForURL(/\/runs\//);

  console.log("4. the agent searches the store's documents");
  await ask(page, "hedgehogs in winter", /Found: .*hibernate/, "5-search.png");

  console.log("5. the agent lists the store's documents with describe_data + query_data");
  await ask(
    page,
    "Which documents are in my store?",
    /Documents in aios.documents: hedgehogs.md \(ready, 3 chunks\)/,
    "6-query-documents.png",
  );

  console.log("DONE");
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
