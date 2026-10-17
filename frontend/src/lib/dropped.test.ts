import { describe, expect, it } from "vitest";

import { confirmed, droppedLabel, purgeIn, removalText } from "@/lib/dropped";

describe("purgeIn", () => {
  const now = new Date("2026-10-17T12:00:00Z");

  it("says how long until it's purged, in the largest whole unit", () => {
    expect(purgeIn("2026-10-20T12:00:00Z", now)).toBe("Purged in 3 days");
    expect(purgeIn("2026-10-18T13:00:00Z", now)).toBe("Purged in 1 day");
    expect(purgeIn("2026-10-17T17:30:00Z", now)).toBe("Purged in 5 hours");
    expect(purgeIn("2026-10-17T12:01:30Z", now)).toBe("Purged in 1 minute");
  });

  it("says it's due once the time has come", () => {
    expect(purgeIn("2026-10-17T12:00:20Z", now)).toBe("Purged within the hour");
    expect(purgeIn("2026-10-16T12:00:00Z", now)).toBe("Purged within the hour");
  });
});

describe("removalText", () => {
  it("names a table's rows and columns", () => {
    expect(
      removalText({ table: "fish", column: null, rows: 3, values: null, columns: ["id", "name"] }),
    ).toBe("Table data.fish: 3 rows, with columns id, name");
  });

  it("names a column's rows and how many have a value", () => {
    expect(
      removalText({ table: "fish", column: "weight_kg", rows: 3, values: 1, columns: null }),
    ).toBe("Column weight_kg of data.fish: a value in 1 of 3 rows");
  });

  it("says when the table isn't there any more", () => {
    expect(removalText({ table: "fish", column: null, rows: null, values: null, columns: null })).toBe(
      "Table data.fish: not there any more",
    );
  });
});

describe("confirmed", () => {
  it("takes exactly the names asked for, ignoring surrounding space", () => {
    expect(confirmed(" fish ", "fish")).toBe(true);
    expect(confirmed("Fish", "fish")).toBe(false);
    expect(confirmed("", "fish")).toBe(false);
  });
});

describe("droppedLabel", () => {
  it("names a table, or a column with its table", () => {
    expect(droppedLabel({ kind: "table", table: "fish", column: null })).toBe("Table data.fish");
    expect(droppedLabel({ kind: "column", table: "fish", column: "name" })).toBe(
      "Column name of data.fish",
    );
  });
});
