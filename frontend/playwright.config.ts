import { defineConfig, devices } from "@playwright/test";

// The browser tests run the app against an in-memory fake of the API (e2e/fake-api.ts), so
// they need no backend, database or sign-in. E2E_PORT moves the dev server, e.g. to run two
// checkouts at once.
const port = Number(process.env.E2E_PORT) || 5174;

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // In CI each shard (`--shard=i/n`) writes a blob report (blob-report/); the frontend gate merges
  // them into one HTML report.
  reporter: process.env.CI ? [["list"], ["blob"]] : "list",
  use: {
    baseURL: `http://localhost:${port}`,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    // A Chromium other than the one this Playwright version downloads, e.g. a preinstalled one.
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined },
  },
  webServer: {
    command: `pnpm exec vite --port ${port} --strictPort`,
    url: `http://localhost:${port}`,
    reuseExistingServer: !process.env.CI,
  },
});
