// Drives the real SPA (vite on :5183, proxied to the Compose API on :58000, which connects to
// Postgres as aios_app): signed in with a session cookie, it creates a skill through the form and
// checks it shows up in the list after a reload.
const path = require("node:path");
const { chromium } = require(
  path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"),
);
const session = require(process.env.SESSION_JSON);

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });
  const context = await browser.newContext();
  await context.addCookies([
    { name: session.cookie_name, value: session.cookie, domain: "localhost", path: "/" },
  ]);
  const page = await context.newPage();
  const name = `ds22 skill ${Date.now()}`;
  await page.goto("http://localhost:5183/skills/new");
  await page.locator("#skill-name").fill(name);
  await page.locator("#skill-content").fill("Created through the UI against aios_app.");
  const saved = page.waitForResponse(
    (r) => r.url().includes("/api/skills") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Create skill" }).click();
  const response = await saved;
  console.log("POST /api/skills ->", response.status());
  await page.goto("http://localhost:5183/skills");
  await page.getByText(name).first().waitFor({ timeout: 10000 });
  console.log("skill listed after reload:", name);
  await page.screenshot({ path: process.env.SCREENSHOT, fullPage: true });
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
