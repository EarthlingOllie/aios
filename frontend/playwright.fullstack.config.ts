import { defineConfig, devices } from "@playwright/test";

// The full-stack tests (fullstack/): the app as Compose's `fullstack` profile serves it, with the
// real API, workers, database, Redis and storage, and the scripted model standing in for the
// providers. No fake API and no dev server. Bring the stack up first (fullstack/README.md);
// FULLSTACK_URL points elsewhere.
const baseURL = process.env.FULLSTACK_URL || "http://localhost:8090";

export default defineConfig({
  testDir: "fullstack",
  // One stack, shared: its tests run one at a time. Each tells its own story, in an organisation
  // of its own, so any test can run in any shard (`--shard=i/n`): CI splits them by test, not
  // by file.
  workers: 1,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  // A whole story through the workers. A step that never comes (a stopped worker) fails here.
  timeout: 120_000,
  expect: { timeout: 30_000 },
  outputDir: "test-results-fullstack",
  // In CI each shard writes a blob report; the full-stack gate merges them into one HTML report.
  reporter: process.env.CI ? [["list"], ["blob", { outputDir: "blob-report-fullstack" }]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined },
  },
});
