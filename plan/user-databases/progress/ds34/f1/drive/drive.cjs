// Finding 1: a user with no runs switches the run history export on in Settings. Does the page
// settle (say when it last exported, and stop asking) or keep "Waiting for the first export…"?
// Usage: node drive.cjs <base url> <out dir> <mode>
//   mode "new":   switch on at once after first sign-in (the store is likely still provisioning)
//   mode "ready": wait for the store to be ready first
const path = require("node:path");
const { chromium } = require(path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"));

const [base, out, mode] = process.argv.slice(2);
const shot = (page, name) => page.screenshot({ path: path.join(out, name), fullPage: true });

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  let asked = 0;
  page.on("request", (r) => {
    if (r.method() === "GET" && new URL(r.url()).pathname === "/api/settings") asked++;
  });
  await page.goto(`${base}/auth/callback?code=x&state=y`);
  await page.waitForURL(`${base}/`);
  if (mode === "ready") {
    for (;;) {
      const r = await page.request.get(`${base}/api/data-store`);
      if ((await r.json()).status === "ready") break;
      await page.waitForTimeout(500);
    }
  }
  await page.goto(`${base}/settings`);
  const status = page.getByRole("status", { name: "Run history export" });
  const store = await (await page.request.get(`${base}/api/data-store`)).json();
  console.log(`mode=${mode}; store status at switch-on: ${store.status}`);
  await page.getByRole("checkbox", { name: "Export my run history to my data store" }).check();
  const t0 = Date.now();
  let last = "";
  let settled = false;
  while (Date.now() - t0 < 150000) {
    const text = await status.textContent();
    if (text !== last) {
      console.log(`${((Date.now() - t0) / 1000).toFixed(1)}s status: ${text}`);
      last = text;
      if (text.startsWith("Waiting")) await shot(page, `settings-${mode}-waiting.png`);
    }
    if (text.startsWith("Last exported")) {
      settled = true;
      break;
    }
    await page.waitForTimeout(500);
  }
  await shot(page, `settings-${mode}-after.png`);
  const before = asked;
  await page.waitForTimeout(4000);
  console.log(`settled=${settled}; GET /api/settings in the 4s after: ${asked - before}; total: ${asked}`);
  console.log("settings:", JSON.stringify(await (await page.request.get(`${base}/api/settings`)).json()));
  await browser.close();
  process.exit(settled ? 0 : 1);
})().catch((e) => {
  console.error(e);
  process.exit(2);
});
