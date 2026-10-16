# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: data-page.spec.ts >> deleting a document removes it from the list and from search
- Location: e2e/data-page.spec.ts:152:1

# Error details

```
Test timeout of 30000ms exceeded.
```

```
Error: expect(locator).toBeVisible() failed

Locator: getByText('No passages matched.')
Expected: visible
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByText('No passages matched.') with timeout 5000ms
  - waiting for getByText('No passages matched.')
  - Protocol error (Runtime.callFunctionOn): Internal server error, session closed.

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
- main:
  - heading "Search" [level=1]
  - paragraph: "Search your documents with the same hybrid retrieval your agents use: semantic matching over the vector index, combined with keyword matching over the full-text index."
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
  - textbox "Search query":
    - /placeholder: Describe what you're looking for, or use keywords, quoted phrases and -exclusions…
    - text: drop
  - text: Results
  - combobox "Results":
    - option "8" [selected]
    - option "20"
    - option "50"
  - text: Enter to search, Shift+Enter for a new line
  - button "Searching…" [disabled]
```

# Test source

```ts
  67  |   await expect(page.getByRole("status", { name: "Data store" })).toBeHidden();
  68  |   await expect(page.getByText("No documents yet")).toBeVisible();
  69  |   await page.getByRole("tab", { name: "Overview" }).click();
  70  |   await expect(overview(page)).toContainText("Ready");
  71  |   expect(api.writes).toEqual([]);
  72  | });
  73  | 
  74  | test("a store that couldn't be set up says so", async ({ page }) => {
  75  |   const api = await fakeApi(page);
  76  |   api.dataStoreStatus = "failed";
  77  |   await page.goto("/data");
  78  | 
  79  |   await expect(overview(page)).toContainText("Failed");
  80  |   await expect(overview(page)).toContainText("Your data store couldn't be set up.");
  81  |   await page.getByRole("tab", { name: "Documents" }).click();
  82  |   await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled();
  83  | });
  84  | 
  85  | test("an empty store invites an upload", async ({ page }) => {
  86  |   await fakeApi(page);
  87  |   await page.goto("/data?tab=documents");
  88  | 
  89  |   await expect(page.getByText("No documents yet.")).toBeVisible();
  90  |   await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  91  |   await expect(documentList(page)).toBeHidden();
  92  | });
  93  | 
  94  | test("lists each document's status, error, chunk count and upload time", async ({ page }) => {
  95  |   const api = await fakeApi(page);
  96  |   api.addDocument("report.pdf", { chunk_count: 12 });
  97  |   api.addDocument("scan.png", { status: "failed", chunk_count: 0, error: "Unsupported image" });
  98  |   api.addDocument("slides.pptx", { status: "processing", chunk_count: 0 });
  99  |   await page.goto("/data?tab=documents");
  100 | 
  101 |   await expect(documentList(page).getByRole("listitem")).toHaveCount(3);
  102 |   await expect(row(page, "report.pdf")).toContainText("Ready");
  103 |   await expect(row(page, "report.pdf")).toContainText("12 chunks");
  104 |   await expect(row(page, "report.pdf")).toContainText("Uploaded Oct 14, 2026");
  105 |   await expect(row(page, "scan.png")).toContainText("Failed");
  106 |   await expect(row(page, "scan.png")).toContainText("Unsupported image");
  107 |   await expect(row(page, "slides.pptx")).toContainText("Processing");
  108 |   await expect(row(page, "slides.pptx")).not.toContainText("chunks");
  109 | 
  110 |   // The worker finishes, and the list follows without a reload.
  111 |   api.finishIngest("slides.pptx", { chunks: 4 });
  112 |   await expect(row(page, "slides.pptx")).toContainText("4 chunks");
  113 |   await expect(row(page, "slides.pptx")).toContainText("Ready");
  114 | });
  115 | 
  116 | test("an upload from the Data page shows its status until it's ready or failed", async ({
  117 |   page,
  118 | }) => {
  119 |   const api = await fakeApi(page);
  120 |   api.ingestAs = "pending";
  121 |   await page.goto("/data?tab=documents");
  122 | 
  123 |   await page.locator('input[type="file"]').setInputFiles([file("notes.md"), file("broken.md")]);
  124 |   await expect(row(page, "notes.md")).toContainText("Waiting");
  125 |   await expect(row(page, "broken.md")).toContainText("Waiting");
  126 |   expect(api.writes.filter(([method]) => method === "POST")).toHaveLength(2);
  127 | 
  128 |   api.finishIngest("notes.md", { chunks: 3 });
  129 |   api.finishIngest("broken.md", { error: "Couldn't read the file" });
  130 |   await expect(row(page, "notes.md")).toContainText("3 chunks");
  131 |   await expect(row(page, "broken.md")).toContainText("Failed");
  132 |   await expect(row(page, "broken.md")).toContainText("Couldn't read the file");
  133 | });
  134 | 
  135 | test("an upload from the chat appears in the Data page's list", async ({ page }) => {
  136 |   const api = await fakeApi(page);
  137 |   api.ingestAs = "pending";
  138 |   await page.goto("/");
  139 | 
  140 |   // The chat keeps its own upload button.
  141 |   await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
  142 |   await page.locator('input[type="file"]').setInputFiles(file("from-chat.md"));
  143 |   await expect(page.getByText("from-chat.md")).toBeVisible();
  144 | 
  145 |   await page.getByRole("link", { name: "Data" }).first().click();
  146 |   await page.getByRole("tab", { name: "Documents" }).click();
  147 |   await expect(row(page, "from-chat.md")).toContainText("Waiting");
  148 |   api.finishIngest("from-chat.md", { chunks: 2 });
  149 |   await expect(row(page, "from-chat.md")).toContainText("2 chunks");
  150 | });
  151 | 
  152 | test("deleting a document removes it from the list and from search", async ({ page }) => {
  153 |   const api = await fakeApi(page);
  154 |   api.addDocument("keep.md");
  155 |   api.addDocument("drop.md");
  156 |   await page.goto("/search?q=drop");
  157 |   await expect(page.getByRole("region", { name: "Results" })).toContainText("drop.md");
  158 | 
  159 |   await page.goto("/data?tab=documents");
  160 |   page.once("dialog", (dialog) => dialog.accept());
  161 |   await page.getByRole("button", { name: "Delete drop.md" }).click();
  162 |   await expect(row(page, "drop.md")).toHaveCount(0);
  163 |   await expect(row(page, "keep.md")).toBeVisible();
  164 |   expect(api.writes).toContainEqual(["DELETE", expect.stringMatching(/^\/api\/documents\/\d+$/), null]);
  165 | 
  166 |   await page.goto("/search?q=drop");
> 167 |   await expect(page.getByText("No passages matched.")).toBeVisible();
      |                                                        ^ Error: expect(locator).toBeVisible() failed
  168 | });
  169 | 
  170 | test("cancelling a delete keeps the document", async ({ page }) => {
  171 |   const api = await fakeApi(page);
  172 |   api.addDocument("keep.md");
  173 |   await page.goto("/data?tab=documents");
  174 | 
  175 |   page.once("dialog", (dialog) => dialog.dismiss());
  176 |   await page.getByRole("button", { name: "Delete keep.md" }).click();
  177 |   await expect(row(page, "keep.md")).toBeVisible();
  178 |   expect(api.writes).toEqual([]);
  179 | });
  180 | 
  181 | test("a slow list shows it's loading, not that there are no documents", async ({ page }) => {
  182 |   const api = await fakeApi(page);
  183 |   api.addDocument("report.pdf");
  184 |   api.documentsDelay = 1500;
  185 |   await page.goto("/data?tab=documents");
  186 | 
  187 |   await expect(page.getByLabel("Loading documents")).toBeVisible();
  188 |   await expect(page.getByText("No documents yet")).toBeHidden();
  189 |   await page.screenshot({ path: test.info().outputPath("loading.png") });
  190 | 
  191 |   await expect(row(page, "report.pdf")).toBeVisible();
  192 |   await expect(page.getByLabel("Loading documents")).toBeHidden();
  193 | });
  194 | 
  195 | test("a busy store says so, and the list loads once it's free", async ({ page }) => {
  196 |   const api = await fakeApi(page);
  197 |   api.addDocument("report.pdf");
  198 |   api.dataStoreBusy = true;
  199 |   await page.goto("/data?tab=documents");
  200 | 
  201 |   await expect(page.getByRole("status", { name: "Documents list" })).toContainText(
  202 |     "Your data store is busy.",
  203 |   );
  204 |   await expect(page.getByText("No documents yet.")).toBeHidden();
  205 | 
  206 |   api.dataStoreBusy = false;
  207 |   await expect(row(page, "report.pdf")).toBeVisible();
  208 |   await expect(page.getByRole("status", { name: "Documents list" })).toBeHidden();
  209 | });
  210 | 
  211 | test("an upload refused by a busy store says so", async ({ page }) => {
  212 |   const api = await fakeApi(page);
  213 |   await page.goto("/data?tab=documents");
  214 |   await expect(page.getByText("No documents yet.")).toBeVisible();
  215 | 
  216 |   api.dataStoreBusy = true;
  217 |   await page.locator('input[type="file"]').setInputFiles(file("notes.md"));
  218 |   await expect(page.getByRole("alert")).toContainText(
  219 |     "notes.md: Your data store is busy. Try again in a moment.",
  220 |   );
  221 | });
  222 | 
```