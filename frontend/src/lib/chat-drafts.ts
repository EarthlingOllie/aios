type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/**
 * Unsent chat messages, per user and chat. Kept in `storage` (the tab's `sessionStorage`), so a
 * draft outlives the chat page: its Edit page, another section, a reload. Each draft is also
 * held in memory, which is all there is when the storage can't be used (blocked, or private
 * browsing).
 */
export function draftStore(storage: DraftStorage | null) {
  const memory = new Map<string, string>();
  const key = (userId: number, chatId: number) => `chat-draft:${userId}:${chatId}`;
  return {
    get(userId: number, chatId: number): string {
      const k = key(userId, chatId);
      if (memory.has(k)) return memory.get(k)!;
      try {
        return storage?.getItem(k) ?? "";
      } catch {
        return "";
      }
    },
    set(userId: number, chatId: number, draft: string) {
      const k = key(userId, chatId);
      // An empty draft is no draft.
      if (draft) memory.set(k, draft);
      else memory.delete(k);
      try {
        if (draft) storage?.setItem(k, draft);
        else storage?.removeItem(k);
      } catch {
        // Kept in memory only.
      }
    },
  };
}

function sessionStorageOrNull(): DraftStorage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export const chatDrafts = draftStore(sessionStorageOrNull());
