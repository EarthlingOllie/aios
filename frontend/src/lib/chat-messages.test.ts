import { describe, expect, it } from "vitest";

import type { ChatMessage } from "@/lib/api";
import { afterCursor, hasOlder, isAtBottom, mergeMessages, MESSAGE_PAGE } from "@/lib/chat-messages";

function message(id: number, body = `message ${id}`): ChatMessage {
  return {
    id,
    chat_id: 1,
    author: { kind: "user", id: 1, name: "Tester", login: "tester", avatar_url: null },
    kind: "text",
    body,
    mentions: [],
    created_at: "2026-10-18T09:00:00Z",
  };
}

const ids = (messages: ChatMessage[]) => messages.map((m) => m.id);

describe("mergeMessages", () => {
  it("adds what's new after what's shown", () => {
    expect(ids(mergeMessages([message(1), message(2)], [message(3), message(5)]))).toEqual([1, 2, 3, 5]);
  });

  it("puts a page of history before what's shown", () => {
    expect(ids(mergeMessages([message(7), message(9)], [message(3), message(4)]))).toEqual([3, 4, 7, 9]);
  });

  it("shows a message once when a poll and a post both bring it", () => {
    const merged = mergeMessages([message(1), message(2, "as posted")], [message(2, "as read"), message(3)]);
    expect(ids(merged)).toEqual([1, 2, 3]);
    // The later read wins.
    expect(merged[1].body).toBe("as read");
  });

  it("keeps the same list when nothing is new, so nothing re-renders", () => {
    const current = [message(1)];
    expect(mergeMessages(current, [])).toBe(current);
  });
});

describe("afterCursor", () => {
  it("is the newest message's id, or 0 for an empty chat", () => {
    expect(afterCursor([message(4), message(8)])).toBe(8);
    expect(afterCursor([])).toBe(0);
  });
});

describe("hasOlder", () => {
  it("is true while a page of history comes back full", () => {
    const full = Array.from({ length: MESSAGE_PAGE }, (_, i) => message(i + 1));
    expect(hasOlder(full)).toBe(true);
    expect(hasOlder(full.slice(1))).toBe(false);
    expect(hasOlder([])).toBe(false);
  });
});

describe("isAtBottom", () => {
  it("counts a view scrolled to within a few pixels of the end as at the bottom", () => {
    expect(isAtBottom({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })).toBe(true);
    expect(isAtBottom({ scrollTop: 590, scrollHeight: 1000, clientHeight: 400 })).toBe(true);
    expect(isAtBottom({ scrollTop: 300, scrollHeight: 1000, clientHeight: 400 })).toBe(false);
  });

  it("counts a view with nothing to scroll as at the bottom", () => {
    expect(isAtBottom({ scrollTop: 0, scrollHeight: 300, clientHeight: 400 })).toBe(true);
  });
});
