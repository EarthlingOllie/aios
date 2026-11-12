import { describe, expect, it } from "vitest";

import type { DocumentInfo, Source, SourceSync } from "@/lib/api";
import {
  canChangeSource,
  canDeleteDocument,
  documentsFrom,
  intervalLabel,
  isPushed,
  parseSenders,
  sourceHealthLabel,
  sourceKindLabel,
  sourceStatusLabel,
  syncSummary,
} from "@/lib/sources";

const uploads = { id: 1, kind: "upload", name: "Uploads" } as const;
const prices = { id: 2, kind: "url", name: "Price page" } as const;
const doc = (id: number, source: DocumentInfo["source"]) => ({ id, source }) as DocumentInfo;

describe("documentsFrom", () => {
  const docs = [doc(1, uploads), doc(2, prices), doc(3, uploads)];

  it("keeps every document when no source is chosen", () => {
    expect(documentsFrom(docs, undefined)).toEqual(docs);
  });

  it("keeps only the chosen source's documents", () => {
    expect(documentsFrom(docs, 1).map((d) => d.id)).toEqual([1, 3]);
    expect(documentsFrom(docs, 2).map((d) => d.id)).toEqual([2]);
    expect(documentsFrom(docs, 9)).toEqual([]);
  });
});

describe("labels", () => {
  it("names each kind and status", () => {
    expect(sourceKindLabel("upload")).toBe("Uploads");
    expect(sourceKindLabel("url")).toBe("Web page");
    expect(sourceKindLabel("connector")).toBe("Connector");
    expect(sourceStatusLabel("active")).toBe("Active");
    expect(sourceStatusLabel("needs_reauth")).toBe("Needs reconnecting");
  });
});

describe("canChangeSource", () => {
  it("never offers to pause or remove Uploads", () => {
    expect(canChangeSource({ kind: "upload" } as Source)).toBe(false);
    expect(canChangeSource({ kind: "url" } as Source)).toBe(true);
  });
});

describe("health", () => {
  it("labels each health", () => {
    expect(sourceHealthLabel("healthy")).toBe("Healthy");
    expect(sourceHealthLabel("delayed")).toBe("Delayed");
    expect(sourceHealthLabel("failing")).toBe("Failing");
    expect(sourceHealthLabel("needs_reconnecting")).toBe("Needs reconnecting");
  });
});

describe("syncSummary", () => {
  const sync = {
    added: 2,
    updated: 1,
    removed: 0,
    skipped: 0,
    dropped: 0,
    awaiting_check: 0,
    storage_full: false,
  } as SourceSync;

  it("says what a sync did, leaving out what it didn't", () => {
    expect(syncSummary(sync)).toBe("2 added · 1 updated");
    expect(syncSummary({ ...sync, added: 0, updated: 0 })).toBe("No changes");
  });

  it("says when the store was full and what waits for Jev", () => {
    expect(syncSummary({ ...sync, skipped: 3, storage_full: true, awaiting_check: 1 })).toBe(
      "2 added · 1 updated · 3 skipped (storage full) · 1 awaiting check",
    );
  });
});

describe("intervalLabel", () => {
  it("says how often a source syncs", () => {
    expect(intervalLabel(900)).toBe("every 15 minutes");
    expect(intervalLabel(3600)).toBe("every hour");
    expect(intervalLabel(7200)).toBe("every 2 hours");
    expect(intervalLabel(null)).toBe(null);
  });
});

describe("canDeleteDocument", () => {
  it("offers to delete only uploads: a synced document is removed at its source", () => {
    expect(canDeleteDocument(doc(1, uploads))).toBe(true);
    expect(canDeleteDocument(doc(2, { id: 3, kind: "connector", name: "Wiki" }))).toBe(false);
  });
});

describe("isPushed", () => {
  it("is true for webhook and email sources, which never sync", () => {
    expect(isPushed("webhook")).toBe(true);
    expect(isPushed("email")).toBe(true);
    expect(isPushed("url")).toBe(false);
    expect(isPushed("connector")).toBe(false);
    expect(isPushed("upload")).toBe(false);
  });
});

describe("parseSenders", () => {
  it("takes one sender per line or comma, leaving out blanks", () => {
    expect(parseSenders("alice@example.com, @example.org\n\n  bob@example.net ,")).toEqual([
      "alice@example.com",
      "@example.org",
      "bob@example.net",
    ]);
    expect(parseSenders("  ")).toEqual([]);
  });
});
