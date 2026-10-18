import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Bot, Workflow } from "lucide-react";

import { ChatComposer } from "@/components/ChatComposer";
import { ErrorBanner, formatDate, POLL_MS, usePoll, useVisible } from "@/components/crud";
import { MarkdownView } from "@/components/Markdown";
import { Button } from "@/components/ui/button";
import { api, describeError, type ChatMessage, type ChatMessagesCursor } from "@/lib/api";
import { afterCursor, hasOlder, isAtBottom, mergeMessages } from "@/lib/chat-messages";

/** How often an open chat asks for what's new while its tab is hidden (ms). */
export const CHAT_HIDDEN_POLL_MS = 30_000;

/** Within this many pixels of the top, scrolling up loads the page of history before. */
const LOAD_OLDER_WITHIN = 48;

/** A chat member's or a message author's picture: a user's avatar (or initial), or an icon. */
export function ChatAvatar({
  kind,
  name,
  avatarUrl,
  alt = "",
}: {
  kind: string;
  name: string;
  avatarUrl: string | null;
  /** What it says to a screen reader; nothing when the name is shown beside it. */
  alt?: string;
}) {
  const label = alt ? { role: "img", "aria-label": alt } : { "aria-hidden": true };
  if (kind === "agent") return <Bot className="text-muted-foreground size-5 shrink-0" {...label} />;
  if (kind === "workflow") return <Workflow className="text-muted-foreground size-5 shrink-0" {...label} />;
  if (avatarUrl) return <img src={avatarUrl} alt={alt} className="size-5 shrink-0 rounded-full" />;
  return (
    <span
      className="bg-muted flex size-5 shrink-0 items-center justify-center rounded-full text-[10px] font-medium uppercase"
      {...label}
    >
      {name.slice(0, 1)}
    </span>
  );
}

/** What a read of new messages does to the scroll: follow them if the reader's at the bottom. */
function followIfAtBottom(el: HTMLElement | null): ScrollIntent {
  return !el || isAtBottom(el) ? { to: "bottom" } : null;
}

/** What the next render does to the list's scroll: go to the bottom, or keep the reader's place. */
type ScrollIntent = { to: "bottom" } | { to: "keep"; scrollTop: number; scrollHeight: number } | null;

/**
 * An open chat's messages and its composer. Shows the latest page, scrolled to the newest, and
 * asks for what's new every `POLL_MS` (`CHAT_HIDDEN_POLL_MS` while the tab is hidden, and at once
 * when it's shown again). Scrolling to the top loads the page before, keeping the reader's place;
 * new messages scroll into view only when the reader is already at the bottom.
 */
export function ChatRoom({
  chatId,
  canPost,
  onPosted,
}: {
  chatId: number;
  /** Members post; anyone else may only read. */
  canPost: boolean;
  /** After a message is posted, e.g. to re-sort the list of chats. */
  onPosted?: () => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>();
  const [older, setOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string>();
  const [draft, setDraft] = useState("");
  const [posting, setPosting] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  // What's shown, as of the last read: polls take their cursor from it.
  const shown = useRef<ChatMessage[]>([]);
  const intent = useRef<ScrollIntent>(null);
  const visible = useVisible();

  /** Shows what a read brought, saying what that does to the scroll. */
  // Nothing new keeps the same list, so nothing renders and the intent waits for the next read.
  const show = useCallback((incoming: ChatMessage[], scroll: ScrollIntent) => {
    shown.current = mergeMessages(shown.current, incoming);
    intent.current = scroll;
    setMessages(shown.current);
  }, []);

  const read = useCallback((cursor: ChatMessagesCursor) => api.listChatMessages(chatId, cursor), [chatId]);

  useEffect(() => {
    read({}).then(
      ({ messages: page }) => {
        setOlder(hasOlder(page));
        show(page, { to: "bottom" });
      },
      (err) => setError(describeError(err)),
    );
  }, [read, show]);

  const loaded = messages !== undefined;
  const poll = useCallback(
    () =>
      read({ after: afterCursor(shown.current) }).then(
        ({ messages: page }) => show(page, followIfAtBottom(list.current)),
        // A failed read is tried again at the next poll.
        () => undefined,
      ),
    [read, show],
  );
  usePoll(loaded, poll, visible ? POLL_MS : CHAT_HIDDEN_POLL_MS);
  const wasVisible = useRef(visible);
  useEffect(() => {
    // Shown again: what was posted meanwhile, now.
    if (loaded && visible && !wasVisible.current) void poll();
    wasVisible.current = visible;
  }, [loaded, visible, poll]);

  const loadOlder = useCallback(async () => {
    if (loadingOlder || !older || shown.current.length === 0) return;
    setLoadingOlder(true);
    try {
      const { messages: page } = await read({ before: shown.current[0].id });
      const el = list.current;
      setOlder(hasOlder(page));
      show(page, el ? { to: "keep", scrollTop: el.scrollTop, scrollHeight: el.scrollHeight } : null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoadingOlder(false);
    }
  }, [loadingOlder, older, read, show]);

  useLayoutEffect(() => {
    const el = list.current;
    const scroll = intent.current;
    intent.current = null;
    if (!el || !scroll) return;
    if (scroll.to === "bottom") el.scrollTop = el.scrollHeight;
    // History went in above: move down by as much, so what the reader was looking at stays put.
    else el.scrollTop = scroll.scrollTop + (el.scrollHeight - scroll.scrollHeight);
  }, [messages]);

  async function onSubmit() {
    const body = draft;
    if (!body.trim() || posting) return;
    setPosting(true);
    setError(undefined);
    try {
      const message = await api.postChatMessage(chatId, body);
      // One's own message always scrolls into view.
      show([message], { to: "bottom" });
      setDraft("");
      onPosted?.();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setPosting(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <div
        ref={list}
        role="log"
        aria-label="Messages"
        // The place is kept by hand when history goes in above (see the layout effect).
        className="flex h-[55vh] min-h-64 flex-col gap-3 overflow-y-auto rounded-md border p-3 [overflow-anchor:none]"
        onScroll={(e) => {
          if (e.currentTarget.scrollTop <= LOAD_OLDER_WITHIN) void loadOlder();
        }}
      >
        {older && (
          <Button variant="ghost" size="sm" className="self-center" disabled={loadingOlder} onClick={loadOlder}>
            {loadingOlder ? "Loading…" : "Load earlier messages"}
          </Button>
        )}
        {messages?.length === 0 && (
          <p className="text-muted-foreground m-auto text-sm">No messages yet. Say hello.</p>
        )}
        {messages?.map((message) => <MessageItem key={message.id} message={message} />)}
      </div>
      {canPost ? (
        <ChatComposer
          id={`chat-${chatId}-composer`}
          placeholder="Write a message"
          value={draft}
          onChange={setDraft}
          busy={false}
          disabled={posting}
          onStop={() => undefined}
          onSubmit={onSubmit}
        />
      ) : (
        <p className="text-muted-foreground text-sm">Only the chat's members can post in it.</p>
      )}
    </div>
  );
}

function MessageItem({ message }: { message: ChatMessage }) {
  const author = message.author;
  const name = author?.name ?? "Deleted user";
  return (
    <article className="flex gap-2">
      <div className="pt-0.5">
        {author ? (
          <ChatAvatar kind={author.kind} name={author.name} avatarUrl={author.avatar_url} alt={author.name} />
        ) : (
          <ChatAvatar kind="user" name="?" avatarUrl={null} />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 text-sm">
          <span className="font-medium">{name}</span>
          <time className="text-muted-foreground text-xs" dateTime={message.created_at}>
            {formatDate(message.created_at)}
          </time>
        </div>
        <MarkdownView className="text-sm">{message.body}</MarkdownView>
      </div>
    </article>
  );
}
