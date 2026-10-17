import { describe, expect, it } from "vitest";

import { plural } from "@/lib/utils";

describe("plural", () => {
  it("adds an s except for exactly one", () => {
    expect(plural(0, "member")).toBe("0 members");
    expect(plural(1, "member")).toBe("1 member");
    expect(plural(3, "node")).toBe("3 nodes");
  });
});
