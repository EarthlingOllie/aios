import { describe, expect, it } from "vitest";

import {
  accessLabel,
  nextSort,
  ownerCandidates,
  rowCountLabel,
  rowRangeLabel,
  shareCandidates,
} from "@/lib/data-tables";

describe("rowCountLabel", () => {
  it("counts rows, and says when the count is an estimate", () => {
    expect(rowCountLabel({ row_count: 0, row_count_estimated: false })).toBe("0 rows");
    expect(rowCountLabel({ row_count: 1, row_count_estimated: false })).toBe("1 row");
    expect(rowCountLabel({ row_count: 1234567, row_count_estimated: true })).toBe(
      "About 1,234,567 rows",
    );
    expect(rowCountLabel({ row_count: null, row_count_estimated: false })).toBeNull();
  });
});

describe("nextSort", () => {
  it("goes ascending, descending, then back to as stored", () => {
    const asc = nextSort(null, "name");
    expect(asc).toEqual({ column: "name", order: "asc" });
    const desc = nextSort(asc, "name");
    expect(desc).toEqual({ column: "name", order: "desc" });
    expect(nextSort(desc, "name")).toBeNull();
  });

  it("starts another column ascending", () => {
    expect(nextSort({ column: "name", order: "desc" }, "id")).toEqual({
      column: "id",
      order: "asc",
    });
  });
});

describe("rowRangeLabel", () => {
  it("names the rows on the page", () => {
    expect(rowRangeLabel(0, 50)).toBe("Rows 1–50");
    expect(rowRangeLabel(1000, 1)).toBe("Row 1,001");
    expect(rowRangeLabel(0, 0)).toBeNull();
  });
});

describe("accessLabel", () => {
  const scribe = { id: 1, name: "Scribe", archived: false };
  it("names the owner, or says there's none, or that it's read-only", () => {
    expect(accessLabel({ owner: null, shares: [] })).toBe("No owner");
    expect(accessLabel({ owner: scribe, shares: [] })).toBe("Owned by Scribe");
    expect(accessLabel({ owner: { ...scribe, archived: true }, shares: [] })).toBe(
      "Read-only: its owner, Scribe, is archived",
    );
  });
});

describe("candidates", () => {
  const agent = (id: number, name: string, archived = false) => ({ id, name, archived });
  const agents = [agent(1, "Scribe"), agent(2, "Helper"), agent(3, "Heir"), agent(4, "Gone", true)];
  const table = { owner: agent(1, "Scribe"), shares: [agent(2, "Helper")] };

  it("offers as owner every live agent but the owner", () => {
    expect(ownerCandidates(agents, table).map((a) => a.name)).toEqual(["Helper", "Heir"]);
  });

  it("offers to share with live agents that neither own it nor have it", () => {
    expect(shareCandidates(agents, table).map((a) => a.name)).toEqual(["Heir"]);
    expect(shareCandidates(agents, { owner: null, shares: [] })).toEqual([]);
  });
});
