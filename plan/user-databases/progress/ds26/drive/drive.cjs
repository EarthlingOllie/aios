// Drives the real SPA (vite, proxied to the real API on the ds26 cluster) through the data
// tools: signs in as a new user (faked GitHub), gives an agent both data tools from the tool
// library, sees them refuse while the store is being set up, starts the real provisioning
// worker, makes a `data.fish` table (standing in for an approved schema change), then asks the
// agent questions it answers with describe_data and query_data against the real store, and
// tries a write, a slow query and another store.
// Usage: node drive.cjs <base url> <out dir> <worker start script> <setup script>
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(
  path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"),
);

const [base, out, startWorker, setupScript] = process.argv.slice(2);
const shot = (page, name) => page.screenshot({ path: path.join(out, name), fullPage: true });

async function ask(page, text, expect, name) {
  await page.goto(page.url());
  const box = page.getByRole("textbox", { name: "Message" });
  await box.fill(text);
  await box.press("Enter");
  const answer = page.getByText(expect, { exact: false }).last();
  await answer.waitFor({ timeout: 30000 });
  console.log(`asked ${JSON.stringify(text)} -> ${JSON.stringify(await answer.innerText())}`);
  // Open every tool call so the screenshot shows inputs and outputs.
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
  const responses = [];
  page.on("response", (r) => {
    if (r.url().includes("/api/")) responses.push(`${r.request().method()} ${r.url()} ${r.status()}`);
  });

  await page.goto(`${base}/auth/callback?code=x&state=y`);
  await page.waitForURL(`${base}/`);
  const upload = page.getByRole("button", { name: "Upload" });
  console.log("upload disabled while provisioning:", await upload.isDisabled());

  // An agent with both data tools, picked from the tool library.
  await page.goto(`${base}/agents/new`);
  await page.locator("#agent-name").fill("Fish analyst");
  await page.getByLabel("Describe data").check();
  await page.getByLabel("Query data").check();
  await shot(page, "1-agent-form-tool-library.png");
  await page.getByRole("button", { name: "Create agent" }).click();
  await page.getByRole("button", { name: "New run" }).waitFor();
  await shot(page, "2-agent-detail.png");
  console.log("agent detail lists tools:", await page.getByText("Describe data").count() > 0,
    await page.getByText("Query data").count() > 0);
  await page.getByRole("button", { name: "New run" }).click();
  await page.waitForURL(/\/runs\//);
  const runUrl = page.url();

  await ask(page, "Which is the heaviest fish?", "The data tools refused", "3-refused-while-setting-up.png");

  // The provisioning worker picks up the queued task.
  const worker = spawn("bash", [startWorker], { stdio: "inherit" });
  await page.goto(`${base}/`);
  const deadline = Date.now() + 120000;
  while (!(await upload.isEnabled())) {
    if (Date.now() > deadline) throw new Error("the store never became ready");
    await page.waitForTimeout(250);
  }
  console.log("store ready; upload enabled");

  const setup = spawnSync(process.env.ENVSH, ["uv", "run", "python", setupScript], {
    cwd: process.env.BACKEND,
    encoding: "utf8",
  });
  if (setup.status !== 0) throw new Error(`setup failed: ${setup.stderr}`);
  const otherDb = setup.stdout.trim().split("\n").pop();
  console.log("made data.fish; other store:", otherDb);

  await page.goto(runUrl);
  await ask(page, "Which is the heaviest fish?", "The heaviest catch is the", "4-heaviest.png");
  await ask(page, "Please delete the fish", "I couldn't delete anything", "5-delete-refused.png");
  await ask(page, "Run something slow", "The slow query was stopped", "6-slow-stopped.png");
  await ask(page, `Read the other store ${otherDb}`, "Reaching another store failed", "7-other-store.png");

  console.log("API responses:\n" + responses.join("\n"));
  await browser.close();
  worker.kill("SIGTERM");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
