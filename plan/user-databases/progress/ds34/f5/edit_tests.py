import os

os.chdir("/home/user/aios/.claude/worktrees/agent-a24cbea290d2f1cca/frontend")


def sub(path, old, new):
    s = open(path).read()
    assert old in s, (path, old[:60])
    open(path, "w").write(s.replace(old, new))


sub(
    "e2e/fake-api.ts",
    """      this.exportedHistoryDeletes++;
      return reply(204);""",
    """      this.exportedHistoryDeletes++;
      // Emptied: nothing exported since (while it's on, the export starts over).
      this.settings.history_exported_at = null;
      return reply(204);""",
)

sub(
    "e2e/settings.spec.ts",
    """  await expect(page.getByText("Exported history deleted.")).toBeVisible();
  expect(api.exportedHistoryDeletes).toBe(1);
});
""",
    """  await expect(page.getByText("Exported history deleted.")).toBeVisible();
  expect(api.exportedHistoryDeletes).toBe(1);
  await expect(page.getByRole("status", { name: "Run history export" })).toHaveText(
    "Off. Your data store has no exported history.",
  );
});

test("deleting exported history while it's on starts the export over", async ({ page }) => {
  const api = await fakeApi(page);
  api.settings = {
    export_run_history: true,
    history_ready: true,
    history_exported_at: "2026-10-16T10:00:00Z",
  };
  await page.goto("/settings");
  const status = page.getByRole("status", { name: "Run history export" });
  await expect(status).toContainText("Last exported");

  let asked = "";
  page.once("dialog", (dialog) => {
    asked = dialog.message();
    return dialog.accept();
  });
  await page.getByRole("button", { name: "Delete exported history" }).click();
  await expect(
    page.getByText("Exported history deleted. All your runs are exported again."),
  ).toBeVisible();
  expect(asked).toContain("switch the export off first");
  expect(api.exportedHistoryDeletes).toBe(1);

  // No longer "Last exported", until the export has copied the runs again.
  await expect(status).toHaveText("Exporting your runs…");
  api.settings.history_exported_at = "2026-10-16T11:00:00Z";
  await expect(status).toContainText("Last exported");
});
""",
)
sub(
    "e2e/settings.spec.ts",
    'await expect(status).toHaveText("Waiting for the first export…");',
    'await expect(status).toHaveText("Exporting your runs…");',
)

sub(
    "src/lib/settings.test.ts",
    """  it("waits for the first export, then says when it last ran", () => {
    const waiting = settings({ export_run_history: true, history_ready: true });
    expect(historyExportStatus(waiting, at)).toBe("Waiting for the first export…");""",
    """  it("says it's exporting until it has (first, or after a delete), then when it last ran", () => {
    const waiting = settings({ export_run_history: true, history_ready: true });
    expect(historyExportStatus(waiting, at)).toBe("Exporting your runs…");""",
)
sub(
    "src/lib/settings.test.ts",
    """    expect(historyExportPending(s)).toBe(false);
  });
});""",
    """    expect(historyExportPending(s)).toBe(false);
  });

  it("says the store has no exported history once it's deleted while off", () => {
    const s = settings({ history_ready: true });
    expect(historyExportStatus(s, at)).toBe("Off. Your data store has no exported history.");
    expect(historyExportPending(s)).toBe(false);
  });
});""",
)
