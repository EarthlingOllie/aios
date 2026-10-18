import type { ChatMessage } from "@/lib/api";

/** How many messages the API returns at most per read (`MESSAGE_PAGE` in `api/chats.py`). */
export const MESSAGE_PAGE = 50;

/**
 * The messages shown plus those a read brought (what's new, a page of history, or a message just
 * posted), in id order, each once: a read's copy replaces the one shown. Returns `current` itself
 * when nothing was brought, so a poll with nothing new doesn't re-render.
 */
export function mergeMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  if (incoming.length === 0) return current;
  const byId = new Map(current.map((m) => [m.id, m]));
  for (const m of incoming) byId.set(m.id, m);
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

/** The `after` cursor for what's new: the newest message's id, or 0 before there are any. */
export function afterCursor(messages: ChatMessage[]): number {
  return messages.length === 0 ? 0 : messages[messages.length - 1].id;
}

/** Whether there may be history before a page the API returned: a short page is all there is. */
export function hasOlder(page: ChatMessage[]): boolean {
  return page.length >= MESSAGE_PAGE;
}

/** How close to the end a view can be and still count as at the bottom, in pixels. */
const BOTTOM_SLACK = 16;

/** Whether a scrolled view is at (or within a few pixels of) its bottom, or has nothing to scroll. */
export function isAtBottom(view: { scrollTop: number; scrollHeight: number; clientHeight: number }) {
  return view.scrollHeight - view.scrollTop - view.clientHeight <= BOTTOM_SLACK;
}
