import { describe, expect, it } from "vitest";

import type { ChatInvocation, ChatMessage } from "@/lib/api";
import {
  bodyShown,
  afterCursor,
  hasOlder,
  isAtBottom,
  isLive,
  mergeInvocations,
  mergeMessages,
  MESSAGE_PAGE,
  newlyRead,
  unreadBadge,
} from "@/lib/chat-messages";

function message(id: number, body = `message ${id}`): ChatMessage {
  return {
    id,
    chat_id: 1,
    author: { kind: "user", id: 1, name: "Tester", login: "tester", avatar_url: null },
    kind: "text",
    body,
    mentions: [],
    invocation_id: null,
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

function invocation(id: number, status: ChatInvocation["status"], extra: Partial<ChatInvocation> = {}): ChatInvocation {
  return {
    id,
    chat_id: 1,
    message_id: 1,
    requested_by: 1,
    target_kind: "workflow",
    target_id: 5,
    status,
    error: null,
    acknowledgement: "Reading the chat…",
    brief: null,
    workflow_run_id: null,
    agent_run_id: null,
    run_deleted: false,
    created_at: "2026-10-18T09:00:00Z",
    started_at: null,
    finished_at: null,
    workflow_run: null,
    agent_run: null,
    ...extra,
  };
}

describe("mergeInvocations", () => {
  it("takes each invocation's state from the latest read, and keeps the rest", () => {
    const shown = { 1: invocation(1, "succeeded"), 2: invocation(2, "queued") };
    const merged = mergeInvocations(shown, [invocation(2, "running"), invocation(3, "queued")]);
    expect(Object.values(merged).map((i) => [i.id, i.status])).toEqual([
      [1, "succeeded"],
      [2, "running"],
      [3, "queued"],
    ]);
    // Not changed in place: the state shown is replaced.
    expect(shown[2].status).toBe("queued");
  });

  it("keeps the same object when a read brings none, so nothing re-renders", () => {
    const shown = { 1: invocation(1, "running") };
    expect(mergeInvocations(shown, [])).toBe(shown);
  });
});

describe("isLive", () => {
  it("is an invocation that hasn't finished: queued, briefing or running", () => {
    expect(
      (["queued", "briefing", "running", "succeeded", "failed"] as const).map((s) => isLive(invocation(1, s))),
    ).toEqual([true, true, true, false, false]);
  });
});

describe("bodyShown", () => {
  const card = { ...message(7, "Reading the chat…"), kind: "run_card" as const, invocation_id: 1 };

  it("is the invocation's latest, once the handoff has said what was understood", () => {
    const briefed = invocation(1, "running", { acknowledgement: "I'll sum up the week." });
    expect(bodyShown(card, briefed)).toBe("I'll sum up the week.");
  });

  it("is the card's own text until its invocation has been read", () => {
    expect(bodyShown(card, undefined)).toBe("Reading the chat…");
  });

  it("is a plain message's own text", () => {
    const reply = { ...message(8, "Done."), invocation_id: 1 };
    expect(bodyShown(reply, invocation(1, "succeeded", { acknowledgement: "On it." }))).toBe("Done.");
  });
});

describe("newlyRead", () => {
  const shown = [message(3), message(5), message(8)];
  const seeing = { atBottom: true, visible: true };

  it("is the newest message once the reader is at the bottom of a visible chat", () => {
    expect(newlyRead(shown, 0, seeing)).toBe(8);
    expect(newlyRead(shown, 5, seeing)).toBe(8);
  });

  it("is nothing new when the newest has been reported already", () => {
    expect(newlyRead(shown, 8, seeing)).toBeNull();
  });

  it("is nothing while the reader is scrolled up or the tab is hidden", () => {
    expect(newlyRead(shown, 0, { atBottom: false, visible: true })).toBeNull();
    expect(newlyRead(shown, 0, { atBottom: true, visible: false })).toBeNull();
  });

  it("is nothing in a chat with no messages", () => {
    expect(newlyRead([], 0, seeing)).toBeNull();
  });
});

describe("unreadBadge", () => {
  it("shows a count, capped, and nothing for none or a chat the user isn't in", () => {
    expect(unreadBadge(0)).toBeNull();
    expect(unreadBadge(null)).toBeNull();
    expect(unreadBadge(1)).toBe("1");
    expect(unreadBadge(99)).toBe("99");
    expect(unreadBadge(100)).toBe("99+");
  });
});
