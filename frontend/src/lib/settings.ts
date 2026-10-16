import type { Settings } from "@/lib/api";

/** What the Settings page says about the run history export. */
export function historyExportStatus(s: Settings, formatDate: (iso: string) => string): string {
  const last = s.history_exported_at ? `Last exported ${formatDate(s.history_exported_at)}.` : null;
  if (s.export_run_history) {
    if (!s.history_ready) return "Setting up the history tables in your data store…";
    // Not exported yet, or deleted and being exported again.
    return last ?? "Exporting your runs…";
  }
  if (!s.history_ready) return "Off. Nothing has been exported.";
  if (!last) return "Off. Your data store has no exported history.";
  return `Off. What was exported is kept. ${last}`;
}

/** Whether the page should ask again soon: the export is on but hasn't caught up yet. */
export function historyExportPending(s: Settings): boolean {
  return s.export_run_history && (!s.history_ready || s.history_exported_at === null);
}
