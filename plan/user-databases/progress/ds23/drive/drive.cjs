// Drives the real SPA (vite, proxied to the real API on the ds23 cluster): signs in as a new
// user (faked GitHub), sees the "being set up" state with Upload disabled, starts the real
// provisioning worker, waits for the store to be ready, uploads a document, and searches it.
// Usage: node drive.cjs <base url> <out dir> <worker start script>
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require(
  path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"),
);

const [base, out, startWorker] = process.argv.slice(2);
const shot = (page, name) => page.screenshot({ path: path.join(out, name), fullPage: true });

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const responses = [];
  page.on("response", (r) => {
    if (r.url().includes("/api/")) responses.push(`${r.request().method()} ${r.url()} ${r.status()}`);
  });

  // Sign up: the faked OAuth callback creates the user and their store, and queues provisioning.
  await page.goto(`${base}/auth/callback?code=x&state=y`);
  await page.waitForURL(`${base}/`);
  const notice = page.getByRole("status", { name: "Data store" });
  await notice.waitFor({ timeout: 15000 });
  console.log("notice:", await notice.innerText());
  const upload = page.getByRole("button", { name: "Upload" });
  console.log("upload disabled while provisioning:", await upload.isDisabled());
  await shot(page, "1-being-set-up.png");

  // Search while it's being set up.
  await page.goto(`${base}/search?q=hedgehogs`);
  await page.getByText("Your data store is being set up").waitFor({ timeout: 10000 });
  console.log("search while provisioning: shows the 409 message");
  await shot(page, "2-search-while-provisioning.png");

  // Now the provisioning worker picks up the queued task.
  const worker = spawn("bash", [startWorker], { stdio: "inherit" });
  await page.goto(`${base}/`);
  await notice.waitFor({ timeout: 15000 });
  const deadline = Date.now() + 120000;
  while (!(await upload.isEnabled())) {
    if (Date.now() > deadline) throw new Error("the store never became ready");
    await page.waitForTimeout(250);
  }
  console.log("notice visible once ready:", await notice.isVisible());
  console.log("notice gone; upload enabled:", await upload.isEnabled());
  await shot(page, "3-ready.png");

  await page.locator('input[type="file"]').setInputFiles({
    name: "hedgehogs.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Hedgehogs\n\nHedgehogs eat beetles and worms at night.\n\nThey hibernate in winter.",
    ),
  });
  await page.getByText(/3 chunks/).waitFor({ timeout: 60000 });
  console.log("uploaded and ingested: hedgehogs.md, 3 chunks");
  await shot(page, "4-uploaded.png");

  await page.goto(`${base}/search?q=what+do+hedgehogs+eat`);
  await page.getByText("beetles", { exact: false }).first().waitFor({ timeout: 20000 });
  console.log("search finds the passage");
  await shot(page, "5-search.png");

  console.log("API responses:\n" + responses.join("\n"));
  await browser.close();
  worker.kill("SIGTERM");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
