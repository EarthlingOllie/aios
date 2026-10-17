import { describe, expect, it } from "vitest";

import type { Settings } from "@/lib/api";
import {
  historyEmbeddingStatus,
  historyExportPending,
  historyExportStatus,
} from "@/lib/settings";

const at = (iso: string) => `<${iso}>`;
const settings = (s: Partial<Settings>): Settings => ({
  export_run_history: false,
  history_ready: false,
  history_exported_at: null,
  embed_run_history: false,
  history_embedded_at: null,
  history_embed_error: null,
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

  it("says it's exporting until it has (first, or after a delete), then when it last ran", () => {
    const waiting = settings({ export_run_history: true, history_ready: true });
    expect(historyExportStatus(waiting, at)).toBe("Exporting your runs…");
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

  it("says the store has no exported history once it's deleted while off", () => {
    const s = settings({ history_ready: true });
    expect(historyExportStatus(s, at)).toBe("Off. Your data store has no exported history.");
    expect(historyExportPending(s)).toBe(false);
  });
});

describe("historyEmbeddingStatus", () => {
  const exported = { export_run_history: true, history_ready: true };
  const done = "2026-10-16T10:00:00Z";

  it("says new messages are found by keyword only while it's off, and what was embedded is kept", () => {
    const s = settings({ ...exported, history_exported_at: done });
    expect(historyEmbeddingStatus(s, at)).toBe(
      "Off. New messages aren't embedded: agents find them by keyword. Those embedded before are kept, and still found by meaning.",
    );
    expect(historyExportPending(s)).toBe(false);
  });

  it("says it's embedding until it has caught up, asking again meanwhile", () => {
    const s = settings({ ...exported, history_exported_at: done, embed_run_history: true });
    expect(historyEmbeddingStatus(s, at)).toBe("Embedding your exported messages…");
    expect(historyExportPending(s)).toBe(true);
  });

  it("says when it last caught up", () => {
    const s = settings({
      ...exported,
      history_exported_at: done,
      embed_run_history: true,
      history_embedded_at: "2026-10-16T11:00:00Z",
    });
    expect(historyEmbeddingStatus(s, at)).toBe(
      "Agents can search your past runs by meaning and by keyword. Last embedded <2026-10-16T11:00:00Z>.",
    );
    expect(historyExportPending(s)).toBe(false);
  });

  it("says why it failed, without asking again every few seconds", () => {
    const s = settings({
      ...exported,
      history_exported_at: done,
      embed_run_history: true,
      history_embed_error: "RuntimeError: embedding API is down",
    });
    expect(historyEmbeddingStatus(s, at)).toBe(
      "Couldn't embed your exported messages (RuntimeError: embedding API is down). " +
        "It's tried again in a few minutes.",
    );
    expect(historyExportPending(s)).toBe(false);
  });
});
