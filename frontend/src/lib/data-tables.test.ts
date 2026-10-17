import { describe, expect, it } from "vitest";

import { nextSort, rowCountLabel, rowRangeLabel } from "@/lib/data-tables";

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
