import { ApiError, describeError, type ChatInvocation, type ChatListed, type ChatMention, type ChatMessage } from "@/lib/api";

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

/** Whether an invocation is still going (its card is live): it hasn't finished (succeeded,
 * failed, or been refused). */
export function isLive(invocation: ChatInvocation): boolean {
  return !["succeeded", "failed", "refused"].includes(invocation.status);
}

/**
 * A run's full output as the card's disclosure shows it: a JSON object or array pretty-printed
 * (`json`), anything else as it is.
 */
export function fullOutputShown(output: string): { text: string; json: boolean } {
  const trimmed = output.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return { text: JSON.stringify(JSON.parse(trimmed), null, 2), json: true };
    } catch {
      // Not JSON after all: shown as it is.
    }
  }
  return { text: output, json: false };
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

/** Why a post was refused, as the room shows it, and what the chat would need to include for it. */
export type PostRefusal = {
  message: string;
  /** The agents and workflows mentioned that the chat doesn't include: **Add it to this chat**. */
  missing: ChatMention[];
};

/**
 * A refused post, from its error: a mention of an agent or workflow the chat doesn't include
 * (`missing`, to offer adding), a message Jev flagged (with Jev's reason), or anything else as
 * `describeError` says it (Jev not answering is "try again").
 */
export function postRefusal(err: unknown): PostRefusal {
  if (err instanceof ApiError) {
    try {
      const detail = JSON.parse(err.message)?.detail;
      if (detail && typeof detail === "object" && typeof detail.message === "string") {
        const message = detail.reason ? `${detail.message} ${detail.reason}` : detail.message;
        return { message, missing: Array.isArray(detail.missing) ? detail.missing : [] };
      }
    } catch {
      // not JSON: described below
    }
  }
  return { message: describeError(err), missing: [] };
}

/** Replies being written, by invocation id: the whole text so far, as the chat's stream sends it. */
export type Drafts = Record<number, string>;

/**
 * The drafts to show, oldest invocation first: a draft goes once its reply is posted (the
 * message replaces it, in the same place at the bottom) or its invocation has finished (a failed
 * turn shows its failure instead). Drafts are never stored: a reload shows the stream's again.
 */
export function shownDrafts(
  drafts: Drafts,
  invocations: Invocations,
  messages: ChatMessage[],
): { invocationId: number; text: string; invocation: ChatInvocation | undefined }[] {
  const replied = new Set(
    messages.filter((m) => m.kind === "text" && m.invocation_id !== null).map((m) => m.invocation_id),
  );
  return Object.entries(drafts)
    .map(([id, text]) => ({ invocationId: Number(id), text, invocation: invocations[Number(id)] }))
    .filter(({ invocationId, text, invocation }) => {
      if (!text || replied.has(invocationId)) return false;
      return invocation === undefined || isLive(invocation);
    })
    .sort((a, b) => a.invocationId - b.invocationId);
}
