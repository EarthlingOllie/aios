import { expect, test } from "@playwright/test";
import { fakeApi } from "./fake-api";
for (const when of ["before", "after"]) {
  test(when, async ({ page }) => {
    const api = await fakeApi(page);
    for (let i = 1; i <= 20; i++) api.addDocument(`doc-${i}.pdf`);
    api.documentsDelay = 3000;
    await page.goto("/data?tab=documents");
    await page.waitForTimeout(800);
    const empty = await page.getByText("No documents yet").isVisible();
    console.log(when, "No documents yet visible while loading:", empty);
    await page.screenshot({ path: "/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds37/f2/" + process.env.SHOT + "-documents-tab-loading.png" });
    const chat = await page.context().newPage();
    await fakeApi(chat).then((a) => { a.addDocument("report.pdf"); a.documentsDelay = 3000; });
    await chat.goto("/");
    await chat.waitForTimeout(800);
    console.log(when, "chat 'Upload files' visible while loading:", await chat.getByText("Upload files to search them in chat.").isVisible());
    await chat.screenshot({ path: "/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds37/f2/" + process.env.SHOT + "-chat-panel-loading.png" });
  });
}
