import { describe, expect, it } from "vitest";

import { draftStore } from "@/lib/chat-drafts";

/** A stand-in for `sessionStorage`. */
function memoryStorage() {
  const items = new Map<string, string>();
  return {
    items,
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => void items.set(key, value),
    removeItem: (key: string) => void items.delete(key),
  };
}

const broken = {
  getItem: () => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("blocked");
  },
  removeItem: () => {
    throw new Error("blocked");
  },
};

describe("draftStore", () => {
  it("keeps a draft per user and chat", () => {
    const storage = memoryStorage();
    const drafts = draftStore(storage);
    drafts.set(1, 7, "Hello");
    drafts.set(1, 8, "Other chat");

    expect(drafts.get(1, 7)).toBe("Hello");
    expect(drafts.get(1, 8)).toBe("Other chat");
    // Another user signed in on the same tab doesn't see it.
    expect(drafts.get(2, 7)).toBe("");
    // It's in the storage, so a new store (a reload) finds it.
    expect(draftStore(storage).get(1, 7)).toBe("Hello");
  });

  it("drops an empty draft", () => {
    const storage = memoryStorage();
    const drafts = draftStore(storage);
    drafts.set(1, 7, "Hello");
    drafts.set(1, 7, "");

    expect(drafts.get(1, 7)).toBe("");
    expect(storage.items.size).toBe(0);
  });

  it("still keeps drafts for the page's life when the storage can't be used", () => {
    for (const storage of [broken, null]) {
      const drafts = draftStore(storage);
      drafts.set(1, 7, "Hello");
      expect(drafts.get(1, 7)).toBe("Hello");
      drafts.set(1, 7, "");
      expect(drafts.get(1, 7)).toBe("");
    }
  });
});
