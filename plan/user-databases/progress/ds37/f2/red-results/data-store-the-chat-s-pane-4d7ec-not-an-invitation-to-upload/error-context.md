# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: data-store.spec.ts >> the chat's panel shows it's loading, not an invitation to upload
- Location: e2e/data-store.spec.ts:65:1

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByLabel('Loading documents')
Expected: visible
Timeout: 5000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByLabel('Loading documents') with timeout 5000ms
  - waiting for getByLabel('Loading documents')

```

```yaml
- complementary:
  - text: Tester
  - button "Sign out"
  - navigation:
    - link "Search":
      - /url: /search
    - link "Insights":
      - /url: /insights
    - link "Chat":
      - /url: /
    - link "Agents":
      - /url: /agents
    - link "Skills":
      - /url: /skills
    - link "Workflows":
      - /url: /workflows
    - link "Scheduler":
      - /url: /scheduler
    - link "Data":
      - /url: /data
    - link "Settings":
      - /url: /settings
  - heading "Documents" [level=2]
  - button "Upload"
  - list:
    - listitem:
      - text: report.pdf 1 chunks
      - button "Delete report.pdf"
- main:
  - paragraph: Ask anything — answers are grounded in your uploaded documents.
  - toolbar:
    - button "Heading"
    - button "Bold"
    - button "Italic"
    - button "Strikethrough"
    - button "Code"
    - button "Link"
    - button "Quote"
    - button "Bulleted list"
    - button "Numbered list"
    - button "Task list"
  - tablist "Editor mode":
    - tab "Write" [selected]
    - tab "Preview"
  - textbox "Message":
    - /placeholder: Message aios…
  - button [disabled]
```

# Test source

```ts
  1  | import { expect, test } from "@playwright/test";
  2  | 
  3  | import { fakeApi } from "./fake-api";
  4  | 
  5  | /** The documents panel while the user's data store is being set up, and once it's ready. */
  6  | 
  7  | test("documents wait for a store that's being set up, then can be uploaded", async ({ page }) => {
  8  |   const api = await fakeApi(page);
  9  |   api.dataStoreStatus = "provisioning";
  10 |   await page.goto("/");
  11 | 
  12 |   const notice = page.getByRole("status", { name: "Data store" });
  13 |   await expect(notice).toContainText("Setting up your data store");
  14 |   const upload = page.getByRole("button", { name: "Upload" });
  15 |   await expect(upload).toBeDisabled();
  16 |   expect(api.writes).toEqual([]);
  17 | 
  18 |   api.dataStoreStatus = "ready";
  19 |   await expect(notice).toBeHidden();
  20 |   await expect(upload).toBeEnabled();
  21 |   await expect(page.getByText("Upload files to search them in chat.")).toBeVisible();
  22 | 
  23 |   await page.locator('input[type="file"]').setInputFiles({
  24 |     name: "notes.md",
  25 |     mimeType: "text/markdown",
  26 |     buffer: Buffer.from("# Notes"),
  27 |   });
  28 |   await expect(page.getByText("notes.md")).toBeVisible();
  29 |   await expect(page.getByText("1 chunks")).toBeVisible();
  30 | });
  31 | 
  32 | test("uploads wait until the store's status is known", async ({ page }) => {
  33 |   const api = await fakeApi(page);
  34 |   api.dataStoreStatus = "ready";
  35 |   api.dataStoreDelay = 1500;
  36 |   await page.goto("/");
  37 | 
  38 |   const upload = page.getByRole("button", { name: "Upload" });
  39 |   await upload.waitFor();
  40 |   expect(await upload.isDisabled()).toBe(true);
  41 |   await expect(upload).toBeEnabled();
  42 | });
  43 | 
  44 | test("a store that couldn't be set up says so", async ({ page }) => {
  45 |   const api = await fakeApi(page);
  46 |   api.dataStoreStatus = "failed";
  47 |   await page.goto("/");
  48 | 
  49 |   await expect(page.getByRole("status", { name: "Data store" })).toContainText(
  50 |     "Your data store couldn't be set up",
  51 |   );
  52 |   await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
  53 | });
  54 | 
  55 | test("searching before the store is ready says it's being set up", async ({ page }) => {
  56 |   const api = await fakeApi(page);
  57 |   api.dataStoreStatus = "provisioning";
  58 |   await page.goto("/search?q=fish");
  59 | 
  60 |   await expect(
  61 |     page.getByText("Your data store is being set up. Try again in a moment."),
  62 |   ).toBeVisible();
  63 | });
  64 | 
  65 | test("the chat's panel shows it's loading, not an invitation to upload", async ({ page }) => {
  66 |   const api = await fakeApi(page);
  67 |   api.addDocument("report.pdf");
  68 |   api.documentsDelay = 1500;
  69 |   await page.goto("/");
  70 | 
> 71 |   await expect(page.getByLabel("Loading documents")).toBeVisible();
     |                                                      ^ Error: expect(locator).toBeVisible() failed
  72 |   await expect(page.getByText("Upload files to search them in chat.")).toBeHidden();
  73 | 
  74 |   await expect(page.getByText("report.pdf")).toBeVisible();
  75 |   await expect(page.getByLabel("Loading documents")).toBeHidden();
  76 | });
  77 | 
```