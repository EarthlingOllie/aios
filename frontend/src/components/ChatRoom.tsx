import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type Dispatch,
  type SetStateAction,
} from "react";

import { ChatComposer, MEMBER_KIND } from "@/components/ChatComposer";
import { ErrorBanner, formatDate, useLoad, usePoll, useVisible } from "@/components/crud";
import { MarkdownView } from "@/components/Markdown";
import { RunCard } from "@/components/RunCard";
import { Button } from "@/components/ui/button";
import {
  api,
  describeError,
  type ChatMemberKind,
  type ChatInvocation,
  type ChatMention,
  type ChatMessage,
  type ChatMessageAuthor,
  type ChatMessages,
  type ChatMessagesCursor,
  type ChatMentionables,
} from "@/lib/api";
import {
  afterCursor,
  bodyShown,
  hasOlder,
  isAtBottom,
  mergeInvocations,
  mergeMessages,
  newlyRead,
  postRefusal,
  shownDrafts,
  type Drafts,
  type Invocations,
  type PostRefusal,
} from "@/lib/chat-messages";
import { useCurrentUser } from "@/lib/current-user";
import { mentionAt, mentionsIn, remarkMentions } from "@/lib/mentions";
import { cn } from "@/lib/utils";
import { FALLBACK_READ_MS, useStream, type StreamEvent } from "@/lib/stream";
import { composerJevNotice } from "@/lib/verdicts";

/** How often the composer asks again whether Jev is up, while the page is visible (ms). */
const JEV_POLL_MS = 60_000;

/** Within this many pixels of the top, scrolling up loads the page of history before. */
const LOAD_OLDER_WITHIN = 48;

/** A chat member's or a message author's picture: a user's initial, or an icon. */
export function ChatAvatar({
  kind,
  name,
  alt = "",
}: {
  kind: ChatMemberKind | ChatMessageAuthor["kind"];
  name: string;
  /** What it says to a screen reader; nothing when the name is shown beside it. */
  alt?: string;
}) {
  const label = alt ? { role: "img", "aria-label": alt } : { "aria-hidden": true };
  if (kind === "agent" || kind === "workflow") {
    const Icon = MEMBER_KIND[kind].icon;
    return <Icon className="text-muted-foreground size-5 shrink-0" {...label} />;
  }
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
 * follows the chat's stream (`lib/stream`): new messages, run cards as their runs move on, and a
 * reply being written, marked "writing…", which the posted reply replaces in place (a failed
 * turn's draft goes, and its failure shows). Nothing polls: a stream that drops catches up from
 * the newest message shown when it opens again, and while it can't open the chat reads what's
 * new every `FALLBACK_READ_MS` until it can. Changes to the chat itself (archived, renamed, its
 * members) go to `onChanged`. Leaving the organisation closes it for good, saying so. Scrolling
 * to the top loads the page before, keeping the reader's place;
 * new messages scroll into view only when the reader is already at the bottom. `@` in the composer
 * offers `mentionable`; a message mentioning the signed-in user is highlighted. A mentioned agent's
 * or workflow's acknowledgement carries a card following its run. Whenever the reader is at the
 * bottom of the visible chat, `onRead` hears of the newest message, once each. The composer's
 * unsent text is held by the caller (`draft`), so it outlives the room, e.g. while another tab
 * is open.
 *
 * Anyone in the organisation reads the chat and posts in it; posting joins them. A refused post
 * stays in the composer, with why: Jev flagged it, Jev couldn't check it (try again), or it
 * mentions an agent or workflow the chat doesn't include, with **Add it to this chat**
 * (`onInclude`), which adds it and sends the message. While Jev is down the composer says so.
 * An archived chat is read-only (`readOnly`): no composer, and the reason in its place.
 */
export function ChatRoom({
  chatId,
  joined,
  readOnly,
  mentionable,
  offered,
  onPosted,
  onInclude,
  onRead,
  onChanged,
  draft,
  onDraftChange,
}: {
  chatId: number;
  /** Whether the reader has joined the chat; posting joins them if not. */
  joined: boolean;
  /** Why nobody can post in it (an archived chat), shown in place of the composer. */
  readOnly?: string | null;
  /** Who can be mentioned: the chat's members, as `GET /api/chats/{id}/mentionable` lists them. */
  mentionable?: ChatMentionables;
  /** Who `@` offers: the members, and the organisation's agents and workflows not in the chat
   * (`composerMentionables`); `mentionable` when not given. */
  offered?: ChatMentionables;
  /** After a message is posted, e.g. to re-sort the list of chats. */
  onPosted?: () => unknown;
  /** **Add it to this chat**: add these agents and workflows, before the message is sent again. */
  onInclude?: (missing: ChatMention[]) => Promise<unknown>;
  /**
   * The reader has seen the messages up to this one (only for a member, who keeps a place). If it
   * fails, the same message is reported again the next time the reader is seen at the bottom.
   */
  onRead?: (messageId: number) => Promise<unknown>;
  /** The chat's header or members changed (it was archived, say): read it again. */
  onChanged?: () => unknown;
  /** What's in the composer, not yet sent. */
  draft: string;
  onDraftChange: Dispatch<SetStateAction<string>>;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>();
  const [invocations, setInvocations] = useState<Invocations>({});
  // Replies being written, from the stream; never stored.
  const [drafts, setDrafts] = useState<Drafts>({});
  const [older, setOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // Set at once, unlike the state: scroll events come faster than renders.
  const loadingOlderNow = useRef(false);
  const [error, setError] = useState<string>();
  // Why the last post was refused; its text is back in the composer.
  const [refusal, setRefusal] = useState<PostRefusal>();
  const [posting, setPosting] = useState(false);
  const { data: jev, reload: reloadJev } = useLoad(api.jevStatus);
  const list = useRef<HTMLDivElement>(null);
  // What's shown, as of the last read: polls take their cursor from it.
  const shown = useRef<ChatMessage[]>([]);
  const intent = useRef<ScrollIntent>(null);
  const visible = useVisible();
  usePoll(visible, reloadJev, JEV_POLL_MS);
  const jevDown = composerJevNotice(jev?.up ?? null);
  // The newest message `onRead` has heard of.
  const reported = useRef(0);

  /** Tells `onRead` of the newest message if the reader has it in view and it hasn't heard. */
  const noteRead = useCallback(() => {
    const el = list.current;
    const seen = newlyRead(shown.current, reported.current, {
      atBottom: !el || isAtBottom(el),
      visible: document.visibilityState !== "hidden",
    });
    if (seen === null || !onRead) return;
    const before = reported.current;
    reported.current = seen;
    onRead(seen).catch(() => {
      // Not noted: unless something newer has been since, it's still to report.
      if (reported.current === seen) reported.current = before;
    });
  }, [onRead]);

  /**
   * Shows what a read brought, saying what that does to the scroll. Nothing new keeps the same
   * list, so nothing renders and the intent is replaced by the next read's.
   */
  const show = useCallback(({ messages: incoming, invocations: states }: ChatMessages, scroll: ScrollIntent) => {
    shown.current = mergeMessages(shown.current, incoming);
    intent.current = scroll;
    setMessages(shown.current);
    setInvocations((current) => mergeInvocations(current, states));
  }, []);

  const read = useCallback((cursor: ChatMessagesCursor) => api.listChatMessages(chatId, cursor), [chatId]);

  useEffect(() => {
    read({}).then(
      (page) => {
        setOlder(hasOlder(page.messages));
        show(page, { to: "bottom" });
      },
      (err) => setError(describeError(err)),
    );
  }, [read, show]);

  const loaded = messages !== undefined;
  const readNew = useCallback(
    () =>
      read({ after: afterCursor(shown.current) }).then(
        (page) => show(page, followIfAtBottom(list.current)),
        // A failed read is tried again at the next one.
        () => undefined,
      ),
    [read, show],
  );
  const onEvent = useCallback(
    ({ event, data }: StreamEvent) => {
      if (event === "message") show({ messages: [data as ChatMessage], invocations: [] }, followIfAtBottom(list.current));
      else if (event === "invocation") setInvocations((current) => mergeInvocations(current, [data as ChatInvocation]));
      else if (event === "draft") {
        const { invocation_id, text } = data as { invocation_id: number; text: string };
        intent.current = followIfAtBottom(list.current);
        setDrafts((current) => ({ ...current, [invocation_id]: text }));
      } else if (event === "chat" || event === "members") onChanged?.();
    },
    [show, onChanged],
  );
  // Opened once the first page is shown, resuming after its newest message.
  const stream = useStream(loaded ? api.chatStreamUrl(chatId) : null, {
    onEvent,
    lastEventId: () => afterCursor(shown.current),
  });
  const leftOrganisation = stream.gone === "left_organisation";
  // While the stream can't open (or was replaced by newer tabs), what's new is read now and then.
  const reading = loaded && !stream.live && !leftOrganisation && stream.gone !== "not_found";
  usePoll(reading, readNew, FALLBACK_READ_MS);
  const writing = useMemo(
    () => shownDrafts(drafts, invocations, messages ?? []),
    [drafts, invocations, messages],
  );

  const loadOlder = useCallback(async () => {
    if (loadingOlderNow.current || !older || shown.current.length === 0) return;
    loadingOlderNow.current = true;
    setLoadingOlder(true);
    try {
      const page = await read({ before: shown.current[0].id });
      const el = list.current;
      setOlder(hasOlder(page.messages));
      show(page, el ? { to: "keep", scrollTop: el.scrollTop, scrollHeight: el.scrollHeight } : null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      loadingOlderNow.current = false;
      setLoadingOlder(false);
    }
  }, [older, read, show]);

  useLayoutEffect(() => {
    const el = list.current;
    const scroll = intent.current;
    intent.current = null;
    if (!el || !scroll) return;
    if (scroll.to === "bottom") el.scrollTop = el.scrollHeight;
    // History went in above: move down by as much, so what the reader was looking at stays put.
    else el.scrollTop = scroll.scrollTop + (el.scrollHeight - scroll.scrollHeight);
  }, [messages, writing]);

  // What's shown now may have been read: after each render of new messages (scrolled by then),
  // on being shown again, and on joining (when there's someone to tell).
  useEffect(() => {
    if (messages !== undefined) noteRead();
  }, [messages, visible, noteRead]);

  async function send(body: string) {
    setPosting(true);
    setError(undefined);
    setRefusal(undefined);
    // What's being sent leaves the composer at once: the draft outlives this room (and `posting`),
    // so left in place it could be sent again from the room shown on coming back to the chat.
    onDraftChange("");
    try {
      // Only those the composer offered are mentions; any other token is plain text.
      const from = offered ?? mentionable;
      const mentions = from ? mentionsIn(body, from) : [];
      // The message, and the acknowledgement and card of each agent or workflow it mentions.
      const posted = await api.postChatMessage(chatId, body, mentions);
      // One's own message always scrolls into view.
      show(posted, { to: "bottom" });
      onPosted?.();
    } catch (err) {
      setRefusal(postRefusal(err));
      // Not sent: back in the composer to try again, unless something else was typed since.
      onDraftChange((current) => current || body);
    } finally {
      setPosting(false);
    }
  }

  async function onSubmit() {
    if (!draft.trim() || posting) return;
    await send(draft);
  }

  /** **Add it to this chat**: add what the refused message needs, then send it as it was. */
  async function include(missing: ChatMention[]) {
    if (!onInclude || posting) return;
    setPosting(true);
    try {
      await onInclude(missing);
    } catch (err) {
      setPosting(false);
      setRefusal({ message: describeError(err), missing });
      return;
    }
    setPosting(false);
    await send(draft);
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
          noteRead();
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
        {messages?.map((message) => (
          <MessageItem
            key={message.id}
            message={message}
            invocation={message.invocation_id === null ? undefined : invocations[message.invocation_id]}
          />
        ))}
        {writing.map(({ invocationId, text, invocation }) => (
          <DraftItem key={`draft-${invocationId}`} text={text} invocation={invocation} messages={messages ?? []} />
        ))}
      </div>
      {leftOrganisation && (
        <p role="status" aria-label="Not live" className="text-muted-foreground text-sm">
          You've left this organisation, so this chat no longer updates.
        </p>
      )}
      {reading && (
        <p role="status" aria-label="Not live" className="text-muted-foreground text-sm">
          Live updates are paused; new messages are checked every few seconds.
        </p>
      )}
      {refusal && (
        <div
          role="alert"
          aria-label="Message not posted"
          className="border-destructive/30 bg-destructive/10 flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm"
        >
          <span className="min-w-0 flex-1">{refusal.message}</span>
          {refusal.missing.length > 0 && onInclude && (
            <Button size="sm" variant="outline" disabled={posting} onClick={() => include(refusal.missing)}>
              Add it to this chat
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setRefusal(undefined)}>
            Dismiss
          </Button>
        </div>
      )}
      {jevDown && (
        <p role="status" aria-label="Messages can't be checked" className="text-destructive text-sm">
          {jevDown}
        </p>
      )}
      {readOnly ? (
        <p role="status" aria-label="Read-only" className="text-muted-foreground text-sm">
          {readOnly}
        </p>
      ) : (
        <>
          {!joined && (
            <p className="text-muted-foreground text-sm">
              Everyone in the organisation can read this chat. Posting in it joins it.
            </p>
          )}
          <ChatComposer
            id={`chat-${chatId}-composer`}
            placeholder="Write a message"
            value={draft}
            onChange={onDraftChange}
            busy={false}
            disabled={posting}
            onStop={() => undefined}
            onSubmit={onSubmit}
            mentionable={offered ?? mentionable}
          />
        </>
      )}
    </div>
  );
}

/**
 * A reply being written, marked "writing…": its agent's, from the card it belongs to. The posted
 * reply takes its place.
 */
function DraftItem({
  text,
  invocation,
  messages,
}: {
  text: string;
  invocation: ChatInvocation | undefined;
  messages: ChatMessage[];
}) {
  const card = messages.find((m) => m.kind === "run_card" && m.invocation_id === invocation?.id);
  const author = card?.author;
  return (
    <article aria-label="Reply being written" className="flex gap-2 opacity-80">
      <div className="pt-0.5">
        <ChatAvatar kind={author?.kind ?? "agent"} name={author?.name ?? ""} alt={author?.name ?? "Agent"} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 text-sm">
          <span className="font-medium">{author?.name ?? "Agent"}</span>
          <span className="text-muted-foreground text-xs italic">writing…</span>
        </div>
        <MarkdownView className="text-sm">{text}</MarkdownView>
      </div>
    </article>
  );
}

function MessageItem({ message, invocation }: { message: ChatMessage; invocation?: ChatInvocation }) {
  const me = useCurrentUser();
  const author = message.author;
  const name = author?.name ?? "Deleted user";
  const mentionsMe = me !== null && message.mentions.some((m) => m.kind === "user" && m.id === me.id);
  const plugins = useMemo(() => [remarkMentions(message.mentions)], [message.mentions]);
  return (
    <article
      className={cn(
        "flex gap-2",
        mentionsMe && "-mx-1.5 rounded-md border-l-2 border-amber-500 bg-amber-50 px-1.5 py-1 dark:bg-amber-950/30",
      )}
    >
      <div className="pt-0.5">
        {author ? (
          <ChatAvatar kind={author.kind} name={author.name} alt={author.name} />
        ) : (
          <ChatAvatar kind="user" name="?" />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 text-sm">
          <span className="font-medium">{name}</span>
          <time className="text-muted-foreground text-xs" dateTime={message.created_at}>
            {formatDate(message.created_at)}
          </time>
          {mentionsMe && <span className="text-xs font-medium text-amber-700 dark:text-amber-400">Mentions you</span>}
        </div>
        <MarkdownView className="text-sm" remarkPlugins={plugins} components={mentionComponents}>
          {bodyShown(message, invocation)}
        </MarkdownView>
        {message.kind === "run_card" && (
          <div className="mt-1">
            <RunCard invocation={invocation} />
          </div>
        )}
      </div>
    </article>
  );
}

/** A mention in a message, as `remarkMentions` marks it: a chip with the member's kind and name. */
function MentionChip({ "data-mention": ref, children, ...props }: ComponentProps<"span"> & { "data-mention"?: string }) {
  const target = ref ? mentionAt(ref) : null;
  if (!target) return <span {...props}>{children}</span>;
  const { label, icon: Icon } = MEMBER_KIND[target.kind];
  return (
    <span
      title={`${label}: ${String(children)}`}
      className="bg-accent text-accent-foreground inline-flex items-baseline gap-1 rounded px-1 font-medium"
    >
      <Icon className="size-3.5 shrink-0 self-center" role="img" aria-label={label} />
      {children}
    </span>
  );
}

const mentionComponents = { span: MentionChip };
