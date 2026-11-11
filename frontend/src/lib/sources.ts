import type { DocumentInfo, Source, SourceKind, SourceStatus } from "@/lib/api";

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

/** Whether a source may be paused or removed at all: never Uploads, so uploading is always on. */
export function canChangeSource(source: Pick<Source, "kind">): boolean {
  return source.kind !== "upload";
}
