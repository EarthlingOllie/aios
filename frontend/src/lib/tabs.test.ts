import { describe, expect, it } from "vitest";

import { activeTab, keyTarget } from "@/lib/tabs";

const ids = ["context", "runs", "versions"];

describe("activeTab", () => {
  it("is the requested tab when there is one", () => {
    expect(activeTab(ids, "runs")).toBe("runs");
  });

  it("is the first tab when none is requested", () => {
    expect(activeTab(ids, null)).toBe("context");
  });

  it("is the first tab when the requested one doesn't exist", () => {
    expect(activeTab(ids, "schedule")).toBe("context");
  });
});

describe("keyTarget", () => {
  it("moves right and left", () => {
    expect(keyTarget("ArrowRight", 0, 3)).toBe(1);
    expect(keyTarget("ArrowLeft", 2, 3)).toBe(1);
  });

  it("wraps around at either end", () => {
    expect(keyTarget("ArrowRight", 2, 3)).toBe(0);
    expect(keyTarget("ArrowLeft", 0, 3)).toBe(2);
  });

  it("jumps to the first and last tab", () => {
    expect(keyTarget("Home", 2, 3)).toBe(0);
    expect(keyTarget("End", 0, 3)).toBe(2);
  });

  it("ignores other keys", () => {
    expect(keyTarget("Enter", 1, 3)).toBeNull();
    expect(keyTarget("ArrowDown", 1, 3)).toBeNull();
  });
});
