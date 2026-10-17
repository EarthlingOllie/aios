import { describe, expect, it } from "vitest";

import { formatBytes } from "@/lib/format-bytes";

describe("formatBytes", () => {
  it("gives small sizes in bytes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1)).toBe("1 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });

  it("gives larger sizes in binary units, to one decimal place below 10", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(10 * 1024)).toBe("10 KB");
    expect(formatBytes(2.5 * 1024 * 1024)).toBe("2.5 MB");
    expect(formatBytes(50 * 1024 * 1024)).toBe("50 MB");
    expect(formatBytes(3 * 1024 ** 3)).toBe("3.0 GB");
  });

  it("moves up a unit rather than round to 1024 of one", () => {
    expect(formatBytes(1024 * 1024 - 1)).toBe("1.0 MB");
  });

  it("drops the decimal for what rounds to 10", () => {
    expect(formatBytes(10 * 1024 - 1)).toBe("10 KB");
  });
});
