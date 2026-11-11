import type { ChatInvocation, ChatListed, ChatMessage } from "@/lib/api";

/** How many messages the API returns at most per read (`MESSAGE_PAGE` in `api/chats/messages.py`). */
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

/** The invocations shown, by id, with those a read brought: a read's state replaces the one shown. */
export type Invocations = Record<number, ChatInvocation>;

/**
 * The invocations shown plus those a read brought, which are as of that read and so replace
 * what's shown. Returns `current` itself when nothing was brought, so a quiet poll doesn't
 * re-render.
 */
export function mergeInvocations(current: Invocations, incoming: ChatInvocation[]): Invocations {
  if (incoming.length === 0) return current;
  const merged = { ...current };
  for (const invocation of incoming) merged[invocation.id] = invocation;
  return merged;
}

/** Whether an invocation is still going (its card is live): it hasn't finished. */
export function isLive(invocation: ChatInvocation): boolean {
  return invocation.status !== "succeeded" && invocation.status !== "failed";
}

/**
 * The text a message shows. A run card's is its acknowledgement as of the latest read of its
 * invocation, which the handoff rewrites (from the placeholder to what the member understood)
 * after the card was read; any other message's is its own.
 */
export function bodyShown(message: ChatMessage, invocation: ChatInvocation | undefined): string {
  return message.kind === "run_card" && invocation ? invocation.acknowledgement : message.body;
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

/**
 * The message to report as read, if there's a newer one than `reported`: the newest shown, once
 * the reader has it in view (at the bottom of a visible chat). Null when there's nothing new to say.
 */
export function newlyRead(
  messages: ChatMessage[],
  reported: number,
  { atBottom, visible }: { atBottom: boolean; visible: boolean },
): number | null {
  const newest = afterCursor(messages);
  return atBottom && visible && newest > reported ? newest : null;
}

/** The most an unread count shows exactly; more is "99+". */
const UNREAD_SHOWN = 99;

/** What a chat's unread badge says: its count (capped), or null for no badge. */
export function unreadBadge(count: number | null): string | null {
  if (!count) return null;
  return count > UNREAD_SHOWN ? `${UNREAD_SHOWN}+` : String(count);
}

/** Where `/` lands: the most recently active chat the user is in (the list comes in that order),
 * or the new-chat form if they're in none. */
export function landingPath(chats: ChatListed[]): string {
  const chat = chats.find((c) => c.is_member);
  return chat ? `/chats/${chat.id}` : "/chats/new";
}
