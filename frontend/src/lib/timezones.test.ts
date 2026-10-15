import { describe, expect, it } from "vitest";

import { browserKnowsTimeZone, formatInTimeZone } from "@/lib/timezones";

const at = "2026-10-15T13:00:00Z";
const format = (timeZone: string) =>
  new Date(at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short", timeZone });

describe("browserKnowsTimeZone", () => {
  it("knows IANA names, but not half-typed or unsupported ones", () => {
    expect(browserKnowsTimeZone("America/New_York")).toBe(true);
    expect(browserKnowsTimeZone("UTC")).toBe(true);
    expect(browserKnowsTimeZone("America/New_Yo")).toBe(false);
    expect(browserKnowsTimeZone("localtime")).toBe(false);
  });
});

describe("formatInTimeZone", () => {
  it("formats in the timezone", () => {
    expect(formatInTimeZone(at, "America/New_York")).toBe(format("America/New_York"));
    expect(formatInTimeZone(at, "America/New_York")).not.toBe(format("UTC"));
  });

  it("falls back to UTC instead of throwing for a timezone the browser doesn't know", () => {
    expect(formatInTimeZone(at, "America/New_Yo")).toBe(format("UTC"));
    expect(formatInTimeZone(at, "localtime")).toBe(format("UTC"));
  });
});
