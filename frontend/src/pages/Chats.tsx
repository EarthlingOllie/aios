import { useCallback, useEffect, useState, type SetStateAction } from "react";
import { Link, Navigate, useNavigate, useOutletContext, useParams } from "react-router";
import { LogIn, LogOut, Pencil, Plus, Trash2, UserMinus } from "lucide-react";

import { MEMBER_KIND } from "@/components/ChatComposer";
import { ChatAvatar, ChatRoom } from "@/components/ChatRoom";
import { ProvenanceLine } from "@/components/provenance";
import {
  DetailPage,
  ErrorBanner,
  Field,
  formatDate,
  MasterDetail,
  Placeholder,
  POLL_MS,
  selectClass,
  useListHref,
  useLoad,
  usePoll,
  useVisible,
  type ListContext,
} from "@/components/crud";
import { Tabs } from "@/components/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  describeError,
  type Chat,
  type ChatInput,
  type ChatListed,
  type ChatMember,
  type ChatMemberKind,
  type ChatMention,
} from "@/lib/api";
import { chatDrafts } from "@/lib/chat-drafts";
import { landingPath, unreadBadge } from "@/lib/chat-messages";
import { composerMentionables } from "@/lib/mentions";
import { useCurrentUser } from "@/lib/current-user";
import { nameAndDescription, type ListFilterGroup } from "@/lib/list-filter";
import { cn } from "@/lib/utils";
import { plural } from "@/lib/utils";

/** How often the chat list asks again, for what's new elsewhere (ms); every 30s while hidden. */
export const CHAT_LIST_POLL_MS = 5 * POLL_MS;
const CHAT_LIST_HIDDEN_POLL_MS = 30_000;

// No sort menu: the list stays most recently active first.
const CHAT_FILTERS: ListFilterGroup<ChatListed>[] = [
  {
    param: "filter",
    label: "Show",
    all: "All",
    options: [
      { key: "joined", label: "Joined", test: (chat) => chat.is_member },
      { key: "not-joined", label: "Not joined", test: (chat) => !chat.is_member },
      { key: "unread", label: "Unread", test: (chat) => (chat.unread_count ?? 0) > 0 },
    ],
  },
];

export function ChatsSection() {
  const { data, error, reload } = useLoad(api.listChats);
  const visible = useVisible();
  // Unread counts go up as others post, in chats other than the one open.
  usePoll(data !== undefined, reload, visible ? CHAT_LIST_POLL_MS : CHAT_LIST_HIDDEN_POLL_MS);
  return (
    <MasterDetail
      title="Chats"
      basePath="/chats"
      items={data}
      error={error}
      empty="No chats yet. A chat is a conversation everyone in the organisation can read; create one and add agents."
      reloadList={reload}
      search={nameAndDescription}
      filters={CHAT_FILTERS}
      renderItem={(chat) => <ChatListItem chat={chat} />}
    />
  );
}

/** A chat in the list: its name, members (or that it's not joined) and unread count. */
function ChatListItem({ chat }: { chat: ChatListed }) {
  const unread = unreadBadge(chat.unread_count);
  return (
    <>
      <div className="flex items-center gap-2">
        <span className={cn("min-w-0 flex-1 truncate font-medium", !chat.is_member && "text-muted-foreground")}>
          {chat.name}
        </span>
        {chat.is_member ? (
          <span className="text-muted-foreground shrink-0 text-xs">{plural(chat.member_count, "member")}</span>
        ) : (
          <Badge variant="outline" className="shrink-0">
            Not joined
          </Badge>
        )}
        {unread && (
          <span
            aria-label={`${unread} unread`}
            className="bg-primary text-primary-foreground shrink-0 rounded-full px-1.5 text-xs font-medium"
          >
            {unread}
          </span>
        )}
      </div>
      {chat.description && <div className="text-muted-foreground truncate text-xs">{chat.description}</div>}
    </>
  );
}

/** `/`: the most recently active chat the user is in, or the new-chat form if they're in none. */
export function ChatsLanding() {
  const { data, error } = useLoad(api.listChats);
  if (error) return <Placeholder>{error}</Placeholder>;
  if (!data) return null;
  return <Navigate to={landingPath(data)} replace />;
}

export function ChatsIndex() {
  return <Placeholder>Select a chat, or create a new one.</Placeholder>;
}

export function ChatDetail() {
  const id = Number(useParams().chatId);
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const { data: chat, error, reload: reloadChat } = useLoad(useCallback(() => api.getChat(id), [id]));
  // Who the composer offers to mention: the members, again whenever they change.
  const { data: mentionable, reload: reloadMentionable } = useLoad(
    useCallback(() => api.listChatMentionable(id), [id]),
  );
  // The organisation's agents and workflows, which the composer offers too: a mention of one the
  // chat doesn't include is refused, with **Add it to this chat**.
  const { data: targets } = useLoad(listTargets);
  const reload = useCallback(
    () => Promise.all([reloadChat(), reloadMentionable()]),
    [reloadChat, reloadMentionable],
  );
  const [actionError, setActionError] = useState<string>();
  // The composer's unsent text, per chat. Held outside the room, which unmounts while the
  // Members tab is open, and outside this page, so it survives the Edit page, another section
  // and a reload; keyed so one chat's (or user's) draft never shows in another.
  const userId = useCurrentUser()?.id ?? 0;
  const [, rerender] = useState(0);
  const draft = chatDrafts.get(userId, id);
  const setDraft = useCallback(
    (update: SetStateAction<string>) => {
      const current = chatDrafts.get(userId, id);
      chatDrafts.set(userId, id, typeof update === "function" ? update(current) : update);
      rerender((n) => n + 1);
    },
    [userId, id],
  );
  // Reading to the bottom clears the chat's unread count in the list.
  const onRead = useCallback(
    (messageId: number) => api.markChatRead(id, messageId).then(reloadList),
    [id, reloadList],
  );

  if (error) return <Placeholder>{error}</Placeholder>;
  if (!chat) return null;

  async function onDelete() {
    if (!window.confirm(`Delete the chat “${chat!.name}”?`)) return;
    try {
      await api.deleteChat(id);
      await reloadList();
      navigate(listHref("/chats"));
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  async function onJoin() {
    try {
      await api.joinChat(id);
      await Promise.all([reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  async function onLeave() {
    if (!window.confirm(`Leave the chat “${chat!.name}”?`)) return;
    try {
      await api.leaveChat(id);
      // Still there to read, and to join again.
      await Promise.all([reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  async function onAdd(kind: ChatMemberKind, targetId: number) {
    try {
      await api.addChatMember(id, { kind, id: targetId });
      await Promise.all([reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  /** **Add it to this chat**: joins them first if need be (only members add), then adds each. */
  async function onInclude(missing: ChatMention[]) {
    if (!chat!.is_member) await api.joinChat(id);
    for (const target of missing) await api.addChatMember(id, { kind: target.kind, id: target.id });
    await Promise.all([reload(), reloadList()]);
  }

  async function onRemove(member: ChatMember) {
    if (!window.confirm(`Remove ${member.name} from the chat?`)) return;
    try {
      await api.removeChatMember(id, member.id);
      await Promise.all([reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  return (
    <DetailPage
      title={chat.name}
      subtitle={
        <>
          {chat.description && <p className="text-foreground whitespace-pre-wrap">{chat.description}</p>}
          <p>
            {plural(chat.member_count, "member")} · Last active {formatDate(chat.updated_at)}
          </p>
          <ProvenanceLine of={chat} />
        </>
      }
      actions={
        chat.is_member ? (
          <>
            <Button asChild variant="outline" size="sm">
              <Link to={listHref(`/chats/${id}/edit`)}>
                <Pencil />
                Edit
              </Link>
            </Button>
            <Button variant="outline" size="sm" onClick={onDelete}>
              <Trash2 />
              Delete
            </Button>
            <Button variant="outline" size="sm" onClick={onLeave}>
              <LogOut />
              Leave
            </Button>
          </>
        ) : (
          <Button size="sm" onClick={onJoin}>
            <LogIn />
            Join
          </Button>
        )
      }
    >
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />
      <Tabs
        label="Chat details"
        tabs={[
          {
            id: "chat",
            label: "Chat",
            content: (
              <ChatRoom
                key={chat.id}
                chatId={chat.id}
                joined={chat.is_member}
                mentionable={mentionable}
                offered={composerMentionables(mentionable, targets)}
                // Posting in a chat they haven't joined joins them.
                onPosted={chat.is_member ? reloadList : () => Promise.all([reload(), reloadList()])}
                onInclude={onInclude}
                onRead={chat.is_member ? onRead : undefined}
                draft={draft}
                onDraftChange={setDraft}
              />
            ),
          },
          {
            id: "members",
            label: "Members",
            count: chat.member_count,
            content: (
              <MembersPanel
                chat={chat}
                onAdd={chat.is_member ? onAdd : undefined}
                onRemove={chat.is_member ? onRemove : undefined}
              />
            ),
          },
        ]}
      />
    </DetailPage>
  );
}

function MembersPanel({
  chat,
  onAdd,
  onRemove,
}: {
  chat: Chat;
  /** Set for its members, who can add the organisation's members, agents and workflows. */
  onAdd?: (kind: ChatMemberKind, id: number) => Promise<void>;
  /** Set for its members, who can remove a member. */
  onRemove?: (member: ChatMember) => void;
}) {
  const groups: { title: string; members: ChatMember[]; empty: string; add?: ChatMemberKind }[] = [
    { title: "Users", members: chat.members.users, empty: "No users.", add: "user" },
    { title: "Agents", members: chat.members.agents, empty: "No agents yet.", add: "agent" },
    { title: "Workflows", members: chat.members.workflows, empty: "No workflows yet.", add: "workflow" },
  ];
  return (
    <div className="flex flex-col gap-4">
      {groups.map(({ title, members, empty, add }) => (
        <div key={title} className="flex flex-col gap-1.5">
          <h4 className="text-sm font-medium">{title}</h4>
          {members.length === 0 ? (
            <p className="text-muted-foreground text-sm">{empty}</p>
          ) : (
            <ul aria-label={title} className="divide-y rounded-md border">
              {members.map((member) => (
                <li key={member.id} className="flex items-center gap-2 px-3 py-2 text-sm">
                  <ChatAvatar
                    kind={member.kind}
                    name={member.name}
                    // The name is beside it; an agent's or workflow's icon says which it is.
                    alt={member.kind === "user" ? "" : MEMBER_KIND[member.kind].label}
                  />
                  <span className="min-w-0 flex-1 truncate">{member.name}</span>
                  {member.archived && <Badge variant="secondary">archived</Badge>}
                  {onRemove && (
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`Remove ${member.name}`}
                      onClick={() => onRemove(member)}
                    >
                      <UserMinus />
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {onAdd && add && (
            <AddMember kind={add} members={members} onAdd={(targetId) => onAdd(add, targetId)} />
          )}
        </div>
      ))}
    </div>
  );
}

/** The organisation's members, as the picker offers them. */
async function listPeople(): Promise<{ id: number; name: string }[]> {
  const { members } = await api.getOrganisation();
  return members.map(({ user }) => ({ id: user.id, name: user.name }));
}

/** The organisation's agents and workflows (not archived), as the composer offers them. */
async function listTargets(): Promise<(ChatMention & { kind: "agent" | "workflow" })[]> {
  const [agents, workflows] = await Promise.all([api.listAgents(), api.listWorkflows()]);
  return [
    ...agents.map((a) => ({ kind: "agent" as const, id: a.id, name: a.name })),
    ...workflows.map((w) => ({ kind: "workflow" as const, id: w.id, name: w.name })),
  ];
}

const ADDABLE: Record<ChatMemberKind, { list: () => Promise<{ id: number; name: string }[]>; a: string }> = {
  user: { list: listPeople, a: "a member" },
  agent: { list: () => api.listAgents(), a: "an agent" },
  workflow: { list: () => api.listWorkflows(), a: "a workflow" },
};

/** A member's picker for adding one of the organisation's members, agents or workflows that
 * isn't in the chat yet. */
function AddMember({
  kind,
  members,
  onAdd,
}: {
  kind: ChatMemberKind;
  members: ChatMember[];
  onAdd: (id: number) => Promise<void>;
}) {
  const { list, a } = ADDABLE[kind];
  const { data: owned } = useLoad<{ id: number; name: string }[]>(list);
  const [chosen, setChosen] = useState("");
  const [adding, setAdding] = useState(false);
  const inChat = new Set(members.map((m) => m.target_id));
  const choices = (owned ?? []).filter((t) => !inChat.has(t.id));
  if (owned && choices.length === 0) return null;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!chosen) return;
    setAdding(true);
    try {
      await onAdd(Number(chosen));
      setChosen("");
    } finally {
      setAdding(false);
    }
  }

  return (
    <form className="flex items-center gap-2" onSubmit={onSubmit}>
      <select
        aria-label={`Add ${a}`}
        className={cn(selectClass, "min-w-0 flex-1")}
        value={chosen}
        onChange={(e) => setChosen(e.target.value)}
      >
        <option value="">Choose {a}</option>
        {choices.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
          </option>
        ))}
      </select>
      <Button type="submit" variant="outline" size="sm" disabled={!chosen || adding}>
        <Plus />
        Add {kind === "user" ? "member" : kind}
      </Button>
    </form>
  );
}

const emptyChat: ChatInput = { name: "", description: "" };

export function ChatForm() {
  const params = useParams();
  const id = params.chatId ? Number(params.chatId) : undefined;
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const [form, setForm] = useState<ChatInput>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(undefined);
    if (id === undefined) setForm(emptyChat);
    else
      api.getChat(id).then(
        ({ name, description }) => setForm({ name, description }),
        (err) => setError(describeError(err)),
      );
  }, [id]);

  if (!form) return error ? <Placeholder>{error}</Placeholder> : null;
  const set = (patch: Partial<ChatInput>) => setForm({ ...form, ...patch });

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      const saved = id === undefined ? await api.createChat(form!) : await api.updateChat(id, form!);
      await reloadList();
      navigate(listHref(`/chats/${saved.id}`));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailPage title={id === undefined ? "New chat" : "Edit chat"}>
      <form className="flex flex-col gap-5" onSubmit={onSubmit}>
        <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        <Field label="Name" htmlFor="chat-name" hint="Unique among the organisation's chats.">
          <Input
            id="chat-name"
            required
            maxLength={255}
            value={form.name}
            onChange={(e) => set({ name: e.target.value })}
            placeholder="e.g. Design"
          />
        </Field>
        <Field label="Description" htmlFor="chat-description">
          <Textarea
            id="chat-description"
            rows={2}
            value={form.description}
            onChange={(e) => set({ description: e.target.value })}
            placeholder="What the chat is for"
          />
        </Field>
        <div className="flex gap-2">
          <Button type="submit" disabled={saving || !form.name.trim()}>
            {id === undefined ? "Create chat" : "Save changes"}
          </Button>
          <Button type="button" variant="ghost" onClick={() => navigate(-1)}>
            Cancel
          </Button>
        </div>
      </form>
    </DetailPage>
  );
}
