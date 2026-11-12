import type { DocumentInfo, Source, SourceHealth, SourceKind, SourceStatus, SourceSync } from "@/lib/api";

/** Sources: what brings content into the organisation's store (the API's `aios.sources`). */

const KIND_LABEL: Record<SourceKind, string> = {
  upload: "Uploads",
  url: "Web page",
  webhook: "Webhook",
  email: "Email",
  connector: "Connector",
};

const STATUS_LABEL: Record<SourceStatus, string> = {
  active: "Active",
  paused: "Paused",
  failing: "Failing",
  needs_reauth: "Needs reconnecting",
  removing: "Being removed",
};

export function sourceKindLabel(kind: SourceKind): string {
  return KIND_LABEL[kind];
}

export function sourceStatusLabel(status: SourceStatus): string {
  return STATUS_LABEL[status];
}

/** The documents that belong to the source `sourceId`, or all of them when none is chosen. */
export function documentsFrom(docs: DocumentInfo[], sourceId: number | undefined): DocumentInfo[] {
  return sourceId === undefined ? docs : docs.filter((d) => d.source.id === sourceId);
}

/** Whether a source is pushed to (webhook, email) rather than synced: it has nothing to sync. */
export function isPushed(kind: SourceKind): boolean {
  return kind === "webhook" || kind === "email";
}

/** Allowed senders as typed: one per line or comma, blanks left out. */
export function parseSenders(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Whether a source may be paused or removed at all: never Uploads, so uploading is always on. */
export function canChangeSource(source: Pick<Source, "kind">): boolean {
  return source.kind !== "upload";
}

const HEALTH_LABEL: Record<SourceHealth, string> = {
  healthy: "Healthy",
  delayed: "Delayed",
  failing: "Failing",
  needs_reconnecting: "Needs reconnecting",
};

export function sourceHealthLabel(health: SourceHealth): string {
  return HEALTH_LABEL[health];
}

/** What a sync did, in a line: its counts (those above zero), whether the store was full, and
 * what still waits for Jev. */
export function syncSummary(
  sync: Pick<SourceSync, "added" | "updated" | "removed" | "skipped" | "dropped" | "awaiting_check" | "storage_full">,
): string {
  const parts: string[] = [];
  if (sync.added) parts.push(`${sync.added} added`);
  if (sync.updated) parts.push(`${sync.updated} updated`);
  if (sync.removed) parts.push(`${sync.removed} removed`);
  if (sync.skipped) parts.push(`${sync.skipped} skipped${sync.storage_full ? " (storage full)" : ""}`);
  if (sync.dropped) parts.push(`${sync.dropped} dropped`);
  if (sync.awaiting_check) parts.push(`${sync.awaiting_check} awaiting check`);
  return parts.length ? parts.join(" · ") : "No changes";
}

/** How often a source syncs, from its interval in seconds; null for one that doesn't (Uploads). */
export function intervalLabel(seconds: number | null): string | null {
  if (seconds === null) return null;
  if (seconds % 3600 === 0) return seconds === 3600 ? "every hour" : `every ${seconds / 3600} hours`;
  const minutes = Math.round(seconds / 60);
  return minutes === 1 ? "every minute" : `every ${minutes} minutes`;
}

/** Whether a document may be deleted here: only an upload. A synced document follows its source,
 * so it's removed there (deleted here, it would come back at the next sync). */
export function canDeleteDocument(doc: Pick<DocumentInfo, "source">): boolean {
  return doc.source.kind === "upload";
}
