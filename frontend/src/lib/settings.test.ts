import { describe, expect, it } from "vitest";

import type { Settings } from "@/lib/api";
import { historyExportPending, historyExportStatus } from "@/lib/settings";

const at = (iso: string) => `<${iso}>`;
const settings = (s: Partial<Settings>): Settings => ({
  export_run_history: false,
  history_ready: false,
  history_exported_at: null,
  ...s,
});

describe("historyExportStatus", () => {
  it("says nothing has been exported before the export was ever on", () => {
    expect(historyExportStatus(settings({}), at)).toBe("Off. Nothing has been exported.");
  });

  it("says the store is being set up until its tables are there", () => {
    const s = settings({ export_run_history: true });
    expect(historyExportStatus(s, at)).toBe("Setting up the history tables in your data store…");
    expect(historyExportPending(s)).toBe(true);
  });

  it("waits for the first export, then says when it last ran", () => {
    const waiting = settings({ export_run_history: true, history_ready: true });
    expect(historyExportStatus(waiting, at)).toBe("Waiting for the first export…");
    expect(historyExportPending(waiting)).toBe(true);
    const done = { ...waiting, history_exported_at: "2026-10-16T10:00:00Z" };
    expect(historyExportStatus(done, at)).toBe("Last exported <2026-10-16T10:00:00Z>.");
    expect(historyExportPending(done)).toBe(false);
  });

  it("says what was exported is kept once it's switched off", () => {
    const s = settings({ history_ready: true, history_exported_at: "2026-10-16T10:00:00Z" });
    expect(historyExportStatus(s, at)).toBe(
      "Off. What was exported is kept. Last exported <2026-10-16T10:00:00Z>.",
    );
    expect(historyExportPending(s)).toBe(false);
  });
});
