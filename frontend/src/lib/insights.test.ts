import { describe, expect, it } from "vitest";

import { daysBetween, formatSeconds, groupName, paramsOf, rangeDays, successRate, tracked, viewOf } from "./insights";

describe("the view in the URL", () => {
  it("reads the range and grouping, falling back to the defaults", () => {
    expect(viewOf(new URLSearchParams("range=7d&group=cause"))).toEqual({ range: "7d", group: "cause" });
    expect(viewOf(new URLSearchParams("range=forever&group=mood"))).toEqual({ range: "30d", group: "agent" });
  });

  it("writes only what isn't the default", () => {
    expect(paramsOf({ range: "30d", group: "agent" }).toString()).toBe("");
    expect(paramsOf({ range: "90d", group: "person" }).toString()).toBe("range=90d&group=person");
  });
});

describe("ranges", () => {
  it("cover today and the days before it", () => {
    const now = Date.parse("2026-11-12T15:00:00Z");
    expect(rangeDays("7d", now)).toEqual({ start: "2026-11-06", end: "2026-11-12" });
    expect(rangeDays("30d", now)).toEqual({ start: "2026-10-14", end: "2026-11-12" });
  });

  it("list every day, both ends included", () => {
    expect(daysBetween("2026-10-30", "2026-11-02")).toEqual(["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]);
  });
});

describe("numbers", () => {
  it("rate success among the finished only", () => {
    expect(successRate(3, 1)).toBe("75%");
    expect(successRate(0, 0)).toBe("–");
  });

  it("say not tracked rather than zero", () => {
    expect(tracked(null)).toBe("Not tracked");
    expect(tracked(0)).toBe("0");
    expect(tracked(12345)).toBe("12,345");
  });

  it("read durations", () => {
    expect(formatSeconds(0.25)).toBe("250 ms");
    expect(formatSeconds(42.4)).toBe("42 s");
    expect(formatSeconds(185)).toBe("3 min 5 s");
    expect(formatSeconds(7800)).toBe("2 h 10 min");
    expect(formatSeconds(null)).toBe("–");
  });

  it("name groups, and what having none means", () => {
    expect(groupName("agent", 3, "Helper")).toBe("Helper");
    expect(groupName("cause", "mention", null)).toBe("Mentions");
    expect(groupName("cause", null, null)).toBe("No cause recorded");
    expect(groupName("person", null, null)).toBe("No one (schedules and events)");
    expect(groupName("workflow", null, null)).toBe("Not in a workflow");
  });
});
