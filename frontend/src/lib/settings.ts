import type { Settings } from "@/lib/api";

/** What the Organisation page says about the run history export. */
export function historyExportStatus(s: Settings, formatDate: (iso: string) => string): string {
  const last = s.history_exported_at ? `Last exported ${formatDate(s.history_exported_at)}.` : null;
  if (s.export_run_history) {
    if (!s.history_ready) return "Setting up the history tables in the data store…";
    // Not exported yet, or deleted and being exported again.
    return last ?? "Exporting the organisation's runs…";
  }
  if (!s.history_ready) return "Off. Nothing has been exported.";
  if (!last) return "Off. The data store has no exported history.";
  return `Off. What was exported is kept. ${last}`;
}

/** What the Organisation page says about embedding the exported messages (shown while the export
 * is on). */
export function historyEmbeddingStatus(s: Settings, formatDate: (iso: string) => string): string {
  if (!s.embed_run_history) {
    return (
      "Off. New messages aren't embedded: agents find them by keyword. " +
      "Those embedded before are kept, and still found by meaning."
    );
  }
  if (s.history_embed_error) {
    return (
      `Couldn't embed the exported messages (${s.history_embed_error}). ` +
      "It's tried again in a few minutes."
    );
  }
  if (!s.history_embedded_at) return "Embedding the exported messages…";
  return (
    "Agents can search past runs by meaning and by keyword. " +
    `Last embedded ${formatDate(s.history_embedded_at)}.`
  );
}

/** Whether the page should ask again soon: the export (or embedding) is on but hasn't caught up
 * yet. */
export function historyExportPending(s: Settings): boolean {
  if (!s.export_run_history) return false;
  return (
    !s.history_ready ||
    s.history_exported_at === null ||
    // Not while it's failed: it's tried again in minutes, not seconds.
    (s.embed_run_history && s.history_embedded_at === null && !s.history_embed_error)
  );
}
