import { describe, expect, it } from "vitest";

import type { DocumentInfo, Source } from "@/lib/api";
import { canChangeSource, documentsFrom, sourceKindLabel, sourceStatusLabel } from "@/lib/sources";

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
