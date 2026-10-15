import { describe, expect, it } from "vitest";

import { formatDate } from "@/components/crud";

describe("formatDate", () => {
  const iso = "2026-03-04T05:06:07Z";

  it("formats as a medium date and short time in the user's locale", () => {
    const expected = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(iso));
    expect(formatDate(iso)).toBe(expected);
  });
});
