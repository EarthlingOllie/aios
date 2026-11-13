import { describe, expect, it } from "vitest";

import { moved } from "./default-chat-agents";

describe("moved", () => {
  it("moves an item up or down, leaving the list it was given alone", () => {
    const list = ["a", "b", "c"];
    expect(moved(list, 2, 1)).toEqual(["a", "c", "b"]);
    expect(moved(list, 0, 1)).toEqual(["b", "a", "c"]);
    expect(list).toEqual(["a", "b", "c"]);
  });

  it("leaves the list as it was for a move out of range", () => {
    expect(moved(["a", "b"], 0, -1)).toEqual(["a", "b"]);
    expect(moved(["a", "b"], 1, 2)).toEqual(["a", "b"]);
  });
});
