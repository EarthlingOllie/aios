// Drives the real app (vite dev server proxying to the real API) through document upload,
// list, search and delete. Writes a normalized transcript (JSON) and screenshots.
// Usage: node drive.mjs <baseURL> <outDir>
import { mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  "/home/user/aios/.claude/worktrees/agent-adf32d3f8ad761786/frontend/package.json",
);
const { chromium } = require("@playwright/test");

const [base, out] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
const log = [];
const norm = (docs) =>
  docs.map(({ filename, status, error, chunk_count }) => ({ filename, status, error, chunk_count }));
const normHits = (hits) =>
  hits.map(({ filename, text, meta, score, semantic_rank, lexical_rank }) => ({
    filename, text, meta, score: Number(score.toFixed(6)), semantic_rank, lexical_rank,
  }));

const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium" });
const page = await browser.newPage();
page.on("console", (m) => m.type() === "error" && log.push({ consoleError: m.text() }));

await page.goto(`${base}/auth/callback?code=x&state=y`);
await page.waitForURL(`${base}/`);
await page.getByText("Upload files to search them in chat.").waitFor();
log.push({ step: "signed in", me: (await (await page.request.get(`${base}/api/me`)).json()).login });

await page.locator('input[type="file"]').setInputFiles([
  {
    name: "fish.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "Zebrafish embryos are transparent.\n\nThe zebrafish genome was sequenced in 2013.",
    ),
  },
  {
    name: "cats.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("Cats purr when content.\n\nA cat sleeps most of the day."),
  },
]);
await page.getByText("2 chunks").first().waitFor();
await page.waitForFunction(() => document.querySelectorAll("li .lucide-file-text").length === 2);
log.push({ step: "uploaded", sidebar: (await page.locator("aside li").allInnerTexts()) });
log.push({ api: "GET /api/documents", body: norm(await (await page.request.get(`${base}/api/documents`)).json()) });
await page.screenshot({ path: `${out}/1-uploaded.png` });

await page.goto(`${base}/search`);
await page.getByLabel("Search query").fill("zebrafish genome");
await page.getByRole("button", { name: "Search", exact: true }).click();
const results = page.getByRole("region", { name: "Results" });
await results.getByText("sequenced").first().waitFor();
log.push({ step: "searched", results: (await results.innerText()).split("\n").filter(Boolean) });
log.push({
  api: "GET /api/search?q=zebrafish genome",
  body: normHits(await (await page.request.get(`${base}/api/search?q=zebrafish%20genome`)).json()),
});
await page.screenshot({ path: `${out}/2-search.png` });

await page.goto(`${base}/`);
await page.getByText("cats.md").hover();
await page.getByLabel("Delete cats.md").click();
await page.getByText("cats.md").waitFor({ state: "detached" });
log.push({ step: "deleted cats.md", sidebar: (await page.locator("aside li").allInnerTexts()) });
log.push({ api: "GET /api/documents", body: norm(await (await page.request.get(`${base}/api/documents`)).json()) });
const del = await page.request.delete(`${base}/api/documents/2147483647`);
log.push({ api: "DELETE missing doc", status: del.status() });
log.push({
  api: "GET /api/search after delete",
  body: normHits(await (await page.request.get(`${base}/api/search?q=cat%20purr`)).json()),
});
await page.screenshot({ path: `${out}/3-deleted.png` });

writeFileSync(`${out}/transcript.json`, JSON.stringify(log, null, 2));
await browser.close();
console.log(JSON.stringify(log, null, 2));
