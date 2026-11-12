import { describe, expect, it } from "vitest";

import type { ChatAction } from "@/lib/api";
import { archivedNotice, can } from "@/lib/chats";

const sam = { id: 2, name: "Sam Admin", former_member: false };

function chat(actions: ChatAction[], archived_by = sam, archived = true) {
  return {
    actions,
    archived_at: archived ? "2026-11-12T09:00:00Z" : null,
    archived_by: archived ? archived_by : null,
  };
}

describe("can", () => {
  it("follows the actions the API says the reader may take", () => {
    const joined = chat(["post", "leave", "edit", "include"], sam, false);
    expect(can(joined, "edit")).toBe(true);
    expect(can(joined, "archive")).toBe(false);
    expect(can(joined, "join")).toBe(false);
  });
});

describe("archivedNotice", () => {
  it("is null for a chat that isn't archived", () => {
    expect(archivedNotice(chat([], sam, false))).toBeNull();
  });

  it("says who archived it, that it's read-only, and that it's still read and searched", () => {
    expect(archivedNotice(chat(["leave", "unarchive"]))).toBe(
      "This chat was archived by Sam Admin. It's read-only: nobody can post in it, join it or change it, but it's still here to read and search.",
    );
    expect(archivedNotice(chat([]), { id: 2 })).toMatch(/^This chat was archived by you\./);
  });

  it("leaves out who when they're no longer known", () => {
    const archived = { ...chat([]), archived_by: null };
    expect(archivedNotice(archived)).toMatch(/^This chat was archived\. It's read-only/);
  });
});
