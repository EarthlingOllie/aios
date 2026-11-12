import { describe, expect, it } from "vitest";

import { amountOf, budgetState, formatPounds, monthLabel } from "@/lib/budget";

describe("formatPounds", () => {
  it("shows pounds to the penny, with thousands separated", () => {
    expect(formatPounds(0)).toBe("£0.00");
    expect(formatPounds(1234.5)).toBe("£1,234.50");
  });
});

describe("amountOf", () => {
  it("reads blank as none, and a sum of pounds as itself", () => {
    expect(amountOf("")).toBeNull();
    expect(amountOf("  ")).toBeNull();
    expect(amountOf("12.5")).toBe(12.5);
    expect(amountOf("0")).toBe(0);
  });

  it("refuses what isn't a sum of pounds", () => {
    expect(amountOf("-1")).toBeUndefined();
    expect(amountOf("lots")).toBeUndefined();
    expect(amountOf("Infinity")).toBeUndefined();
  });
});

describe("budgetState", () => {
  it("says nothing with no budget", () => {
    expect(budgetState(50, null, 0.8)).toEqual({ share: null, level: "none" });
  });

  it("is fine below the warning threshold, warned from it, and reached at 100%", () => {
    expect(budgetState(7.9, 10, 0.8)).toEqual({ share: 0.79, level: "ok" });
    expect(budgetState(8, 10, 0.8)).toEqual({ share: 0.8, level: "warning" });
    expect(budgetState(10, 10, 0.8)).toEqual({ share: 1, level: "reached" });
    expect(budgetState(12, 10, 0.8).level).toBe("reached");
  });

  it("counts a budget of nothing as reached", () => {
    expect(budgetState(0, 0, 0.8).level).toBe("reached");
  });
});

describe("monthLabel", () => {
  it("names the calendar month", () => {
    expect(monthLabel("2026-11")).toBe("November 2026");
  });
});
