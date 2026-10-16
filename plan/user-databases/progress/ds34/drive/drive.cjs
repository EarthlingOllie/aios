// Drives the real SPA (vite, proxied to the real API on the ds34 cluster, with the real
// provisioning worker and a real app worker): signs in, chats with an agent, switches the run
// history export on in Settings (backfill), sends a new turn and sees it in the store, deletes a
// run and sees its copy go, switches the export off (copy kept), and deletes exported history.
// Usage: node drive.cjs <base url> <out dir>
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require(
  path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"),
);

const [base, out] = process.argv.slice(2);
const shot = (page, name) => page.screenshot({ path: path.join(out, name), fullPage: true });
const store = (label) => {
  const text = execFileSync("uv", ["run", "python", process.env.STORE_PY, label], {
    cwd: process.env.BACKEND,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  console.log(text);
  return text;
};
async function until(what, check, ms = 60000) {
  const deadline = Date.now() + ms;
  for (;;) {
    if (await check()) return;
    if (Date.now() > deadline) throw new Error(`timed out: ${what}`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  const responses = [];
  page.on("response", (r) => {
    if (r.url().includes("/api/")) responses.push(`${r.request().method()} ${r.url()} ${r.status()}`);
  });

  await page.goto(`${base}/auth/callback?code=x&state=y`);
  await page.waitForURL(`${base}/`);
  await until("store ready", async () => {
    const r = await page.request.get(`${base}/api/data-store`);
    return (await r.json()).status === "ready";
  }, 120000);
  console.log("store ready");

  // An agent, with a run from before the export.
  const agent = await (
    await page.request.post(`${base}/api/agents`, {
      data: { name: "Echo", description: "", base_prompt: "Echo.", skill_version_ids: [], tools: [] },
    })
  ).json();
  const runUrl = async () => {
    const run = await (await page.request.post(`${base}/api/agents/${agent.id}/runs`)).json();
    return [run.id, `${base}/agents/${agent.id}/runs/${run.id}`];
  };
  const say = async (url, message) => {
    await page.goto(url);
    await page.getByRole("textbox", { name: "Message" }).fill(message);
    await page.keyboard.press("Enter");
    await page.getByText(`echo: ${message}`).waitFor({ timeout: 30000 });
  };
  const [firstId, first] = await runUrl();
  await say(first, "before the export");
  const [secondId, second] = await runUrl();
  await say(second, "another old run");
  console.log(`runs before the export: ${firstId}, ${secondId}`);

  // Settings: off, then on.
  await page.goto(`${base}/settings`);
  const toggle = page.getByRole("checkbox", { name: "Export my run history to my data store" });
  const status = page.getByRole("status", { name: "Run history export" });
  await status.waitFor();
  console.log("settings, off:", await status.innerText());
  await shot(page, "1-settings-off.png");
  await toggle.check();
  console.log("switched on:", await status.innerText());
  await shot(page, "2-switched-on.png");
  await page.getByText("Last exported").waitFor({ timeout: 90000 });
  console.log("after set-up and backfill:", await status.innerText());
  await shot(page, "3-backfilled.png");
  const backfilled = store("after backfill");
  if (!backfilled.includes("before the export") || !backfilled.includes("another old run"))
    throw new Error("backfill missing");

  // A new turn appears in the store shortly after it ends.
  const t0 = Date.now();
  await say(first, "a new turn");
  await shot(page, "4-new-turn.png");
  await until("new turn in the store", async () => store("polling for the new turn").includes("a new turn"), 60000);
  console.log(`new turn exported within ${((Date.now() - t0) / 1000).toFixed(1)}s of being sent`);

  // Deleting a run removes its copy.
  await page.goto(second);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete run" }).click();
  await page.waitForURL(new RegExp(`/agents/${agent.id}([?]|$)`));
  await until("deleted run's copy gone", async () => !store("polling for the delete").includes("another old run"));
  console.log("deleted run's copy is gone");

  // Off: the copy stays, and new turns aren't exported.
  await page.goto(`${base}/settings`);
  await toggle.uncheck();
  await page.getByText("Off. What was exported is kept.").waitFor();
  console.log("switched off:", await status.innerText());
  await shot(page, "5-switched-off.png");
  await say(first, "while it is off");
  await new Promise((r) => setTimeout(r, 5000));
  const kept = store("after switching off and another turn");
  if (!kept.includes("a new turn") || kept.includes("while it is off"))
    throw new Error("switching off didn't keep the copy, or kept exporting");

  // Delete exported history.
  await page.goto(`${base}/settings`);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete exported history" }).click();
  await page.getByText("Exported history deleted.").waitFor();
  await shot(page, "6-deleted.png");
  const emptied = store("after delete exported history");
  if (emptied.includes("('"+"echo") || emptied.match(/^ {3}\(/m)) throw new Error("history not emptied");
  console.log("exported history deleted: the tables are empty");

  // The setting persists across a reload.
  await page.reload();
  await status.waitFor();
  console.log("after reload, checked:", await toggle.isChecked());

  console.log("API responses:\n" + responses.join("\n"));
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
