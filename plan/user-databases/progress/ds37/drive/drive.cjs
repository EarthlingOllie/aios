// Drives the real SPA (vite, proxied to the real API on the ds37 cluster) for #37, the Data page:
// a new user's store is being set up (Overview says so, Documents can't upload), then ready;
// uploads from the Data page and from the chat land in the Documents list, which follows the
// real ingest worker to ready or failed; deleting one removes it from the list and from search;
// each section has its own URL.
// Usage: node drive.cjs <base url> <out dir>
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(
  path.join(process.env.FRONTEND, "node_modules", "@playwright", "test"),
);

const [base, out] = process.argv.slice(2);
const shot = (page, name) => page.screenshot({ path: path.join(out, name), fullPage: true });
const check = (ok, what) => {
  if (!ok) throw new Error(`FAILED: ${what}`);
  console.log(`   ok: ${what}`);
};

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on("response", (r) => {
    if (r.url().includes("/api/") && !r.url().includes("/api/chat"))
      console.log(`  ${r.request().method()} ${r.url().replace(base, "")} ${r.status()}`);
  });
  page.on("dialog", (d) => d.accept());
  const row = (filename) =>
    page.getByRole("list", { name: "Documents" }).getByRole("listitem").filter({ hasText: filename });
  const overview = page.getByRole("region", { name: "Data store" });
  const upload = page.getByRole("button", { name: "Upload" });

  console.log("1. a new user signs in; the Data nav item opens the Overview: being set up");
  await page.goto(`${base}/auth/callback?code=-3700&state=y`);
  await page.waitForURL(`${base}/`);
  await page.getByRole("link", { name: "Data" }).first().click();
  await page.waitForURL(`${base}/data`);
  await overview.getByText("Being set up").waitFor();
  check(
    (await overview.innerText()).includes("We're setting up your data store."),
    "Overview says the store is being set up",
  );
  await shot(page, "1-overview-provisioning.png");

  console.log("2. Documents while it's being set up: no upload");
  await page.getByRole("tab", { name: "Documents" }).click();
  await page.waitForURL(`${base}/data?tab=documents`);
  await page.getByRole("status", { name: "Data store" }).waitFor();
  check(await upload.isDisabled(), "Upload is disabled while the store is set up");
  await shot(page, "2-documents-provisioning.png");

  console.log("3. the provisioning worker starts; the page sees the store become ready");
  fs.writeFileSync(path.join(out, "start-provisioning"), "");
  await upload.waitFor();
  await page.waitForFunction(
    () => !document.querySelector("button:disabled")?.textContent?.includes("Upload"),
    null,
    { timeout: 120000 },
  );
  await page.getByText("No documents yet.").waitFor();
  check(await upload.isEnabled(), "Upload is enabled once the store is ready");
  await shot(page, "3-documents-empty.png");
  await page.getByRole("tab", { name: "Overview" }).click();
  await page.waitForURL(`${base}/data`);
  await overview.getByText("Ready", { exact: true }).waitFor();
  await shot(page, "4-overview-ready.png");

  console.log("4. upload two files from the Data page: one ingests, one fails");
  await page.goto(`${base}/data?tab=documents`);
  await page.locator('input[type="file"]').setInputFiles([
    {
      name: "hedgehogs.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(
        "# Hedgehogs\n\nHedgehogs eat beetles and worms at night.\n\nThey hibernate in winter.",
      ),
    },
    { name: "broken.md", mimeType: "text/markdown", buffer: Buffer.from([0xff, 0xfe, 0x00, 0x81]) },
  ]);
  await row("hedgehogs.md").waitFor();
  const early = await row("hedgehogs.md").innerText();
  check(/Waiting|Processing/.test(early), `hedgehogs.md shows it's in progress (${early.replace(/\n/g, " / ")})`);
  await shot(page, "5-documents-ingesting.png");
  await row("hedgehogs.md").getByText("3 chunks").waitFor({ timeout: 60000 });
  await row("broken.md").getByText("Failed", { exact: true }).waitFor({ timeout: 60000 });
  console.log("   broken.md:", (await row("broken.md").innerText()).replace(/\n/g, " / "));
  check((await row("broken.md").innerText()).includes("UnicodeDecodeError"), "the failure's error is shown");
  check((await row("hedgehogs.md").innerText()).includes("Uploaded "), "the upload time is shown");
  await shot(page, "6-documents-ready-and-failed.png");

  console.log("5. upload from the chat's panel; it appears on the Data page and follows its ingest");
  await page.goto(`${base}/`);
  await upload.waitFor();
  await page.waitForFunction(() =>
    [...document.querySelectorAll("button")].some((b) => b.textContent.includes("Upload") && !b.disabled),
  );
  await page.locator('input[type="file"]').setInputFiles({
    name: "otters.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Otters\n\nOtters hold hands while they sleep."),
  });
  await page.getByText("otters.md").waitFor();
  await shot(page, "7-chat-upload.png");
  await page.getByRole("link", { name: "Data" }).first().click();
  await page.getByRole("tab", { name: "Documents" }).click();
  await row("otters.md").waitFor();
  console.log("   otters.md on the Data page:", (await row("otters.md").innerText()).replace(/\n/g, " / "));
  await row("otters.md").getByText("2 chunks").waitFor({ timeout: 60000 });
  check(true, "otters.md reached ready on the Data page without a reload");
  await shot(page, "8-documents-with-chat-upload.png");

  console.log("6. delete hedgehogs.md: gone from the list and from search");
  await page.goto(`${base}/search?q=hedgehogs+eat+beetles`);
  await page.getByText("beetles").first().waitFor({ timeout: 20000 });
  await shot(page, "9-search-before-delete.png");
  await page.goto(`${base}/data?tab=documents`);
  await row("hedgehogs.md").waitFor();
  await page.getByRole("button", { name: "Delete hedgehogs.md" }).click();
  await row("hedgehogs.md").waitFor({ state: "detached" });
  check((await row("otters.md").count()) === 1, "the other documents stay");
  await shot(page, "10-documents-after-delete.png");
  await page.goto(`${base}/search?q=hedgehogs+eat+beetles`);
  await page.getByText(/No passages matched|passage/).first().waitFor({ timeout: 20000 });
  const hits = await (await page.request.get(`${base}/api/search?q=hedgehogs+beetles`)).json();
  console.log("   search hits after delete:", JSON.stringify(hits.map((h) => h.filename)));
  check(!hits.some((h) => h.filename === "hedgehogs.md"), "search no longer finds hedgehogs.md");
  await shot(page, "11-search-after-delete.png");

  console.log("7. each section's URL opens it directly");
  await page.goto(`${base}/data?tab=documents`);
  check(
    (await page.getByRole("tab", { name: "Documents" }).getAttribute("aria-selected")) === "true",
    "/data?tab=documents opens Documents",
  );
  await page.goto(`${base}/data`);
  check(
    (await page.getByRole("tab", { name: "Overview" }).getAttribute("aria-selected")) === "true",
    "/data opens the Overview",
  );

  console.log("8. a phone-sized screen");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/data?tab=documents`);
  await row("otters.md").waitFor();
  await shot(page, "12-documents-phone.png");

  console.log("DONE");
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
