// Signs in as the setup's user and watches the documents panel: is legacy.md still "pending",
// and does the 2s busy poll keep asking for /api/documents?
// Usage: node drive.cjs <base url> <out dir> <label>
const path = require("node:path");
const { chromium } = require(path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"));
const [base, out, label] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const lists = [];
  page.on("response", (r) => {
    const u = new URL(r.url());
    if (r.request().method() === "GET" && u.pathname === "/api/documents") lists.push(Date.now());
  });
  await page.goto(`${base}/auth/callback?code=x&state=y`);
  await page.waitForURL(`${base}/`);
  const row = page.getByText("legacy.md");
  await row.waitFor({ timeout: 15000 });
  const start = Date.now();
  await page.waitForTimeout(10000);
  const inWindow = lists.filter((t) => t >= start).length;
  const item = row.locator("xpath=ancestor::*[@title or self::li][1]");
  console.log(`[${label}] row:`, (await item.innerText()).replace(/\s+/g, " "));
  console.log(`[${label}] row title (error):`, await item.getAttribute("title"));
  console.log(`[${label}] GET /api/documents in the 10s after the list loaded:`, inWindow);
  await page.screenshot({ path: path.join(out, `${label}.png`), fullPage: true });
  await browser.close();
})().catch((err) => { console.error(err); process.exit(1); });
