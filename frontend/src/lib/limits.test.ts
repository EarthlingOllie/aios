import { describe, expect, it } from "vitest";

import {
  aboveCeiling,
  draftOf,
  formatCost,
  formatLimit,
  invocableByLabel,
  limitsOf,
  RUN_LIMITS,
  TURN_LIMITS,
} from "@/lib/limits";

describe("formatLimit", () => {
  it("shows a cost in pounds and a count with separators", () => {
    expect(formatLimit("turn_cost", 25)).toBe("£25.00");
    expect(formatLimit("turn_tokens", 5_000_000)).toBe("5,000,000");
    expect(formatLimit("run_model_calls", 2000)).toBe("2,000");
  });
});

describe("formatCost", () => {
  it("is pounds, to the penny, or finer for small amounts", () => {
    expect(formatCost(12.5)).toBe("£12.50");
    expect(formatCost(0.0042)).toBe("£0.0042");
    expect(formatCost(0)).toBe("£0.00");
  });

  it("says when nothing was priced", () => {
    expect(formatCost(null)).toBe("No price");
  });
});

describe("drafts", () => {
  it("are the caps as typed: blank follows the organisation's", () => {
    const draft = draftOf(TURN_LIMITS, { turn_model_calls: 3, turn_tokens: null, turn_cost: 1.5 });
    expect(draft).toEqual({ turn_model_calls: "3", turn_tokens: "", turn_cost: "1.5" });
    expect(limitsOf(TURN_LIMITS, draft)).toEqual({ turn_model_calls: 3, turn_tokens: null, turn_cost: 1.5 });
  });

  it("are invalid with a count that isn't a whole number, or anything not above zero", () => {
    expect(limitsOf(RUN_LIMITS, { run_model_calls: "2.5", run_cost: "" })).toBeUndefined();
    expect(limitsOf(RUN_LIMITS, { run_model_calls: "", run_cost: "0" })).toBeUndefined();
    expect(limitsOf(RUN_LIMITS, { run_model_calls: "abc", run_cost: "" })).toBeUndefined();
  });
});

describe("aboveCeiling", () => {
  const ceiling = { turn_model_calls: 200, turn_tokens: 5_000_000, turn_cost: 25, run_model_calls: 2000, run_cost: 100 };

  it("names the caps set above the organisation's, unless they were already", () => {
    expect(aboveCeiling({ turn_model_calls: 300, turn_tokens: null, turn_cost: 10 }, ceiling)).toEqual([
      "turn_model_calls",
    ]);
    expect(aboveCeiling({ turn_model_calls: 300 }, ceiling, { turn_model_calls: 300 })).toEqual([]);
    expect(aboveCeiling({ run_cost: 100 }, ceiling)).toEqual([]);
  });
});

describe("invocableByLabel", () => {
  it("says who may mention it", () => {
    expect(invocableByLabel("everyone")).toBe("Everyone");
    expect(invocableByLabel("editors")).toBe("Editors and admins");
    expect(invocableByLabel("admins")).toBe("Admins only");
  });
});
