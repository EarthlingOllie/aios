import { useCallback, useEffect, useState } from "react";
import { Link, Navigate, useNavigate, useOutletContext, useParams } from "react-router";
import { LogIn, LogOut, Lock, Pencil, Plus, Trash2, UserMinus } from "lucide-react";

import { MEMBER_KIND } from "@/components/ChatComposer";
import { ChatAvatar, ChatRoom } from "@/components/ChatRoom";
import {
  DetailPage,
  ErrorBanner,
  Field,
  formatDate,
  MasterDetail,
  Placeholder,
  POLL_MS,
  selectClass,
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
  type ChatTargetKind,
  type ChatVisibility,
} from "@/lib/api";
import { landingPath, unreadBadge } from "@/lib/chat-messages";
import { cn } from "@/lib/utils";
import { plural } from "@/lib/utils";

const VISIBILITY: Record<ChatVisibility, { label: string; hint: string }> = {
  public: { label: "Public", hint: "Anyone signed in can see it." },
  private: { label: "Private", hint: "Only its members can see it." },
};

/** How often the chat list asks again, for what's new elsewhere (ms); every 30s while hidden. */
export const CHAT_LIST_POLL_MS = 5 * POLL_MS;
const CHAT_LIST_HIDDEN_POLL_MS = 30_000;

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
      empty="No chats yet. A chat is a group conversation; create one and add members."
      reloadList={reload}
      renderItem={(chat) => <ChatListItem chat={chat} />}
    />
  );
}

/** A chat in the list: its name, privacy, members (or that it's not joined) and unread count. */
function ChatListItem({ chat }: { chat: ChatListed }) {
  const unread = unreadBadge(chat.unread_count);
  return (
    <>
      <div className="flex items-center gap-2">
        <span className={cn("min-w-0 flex-1 truncate font-medium", !chat.is_member && "text-muted-foreground")}>
          {chat.name}
        </span>
        {chat.visibility === "private" && (
          <Lock className="text-muted-foreground size-3.5 shrink-0" role="img" aria-label="Private" />
        )}
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
  const { reloadList } = useOutletContext<ListContext>();
  const { data: chat, error, reload: reloadChat } = useLoad(useCallback(() => api.getChat(id), [id]));
  // Who the composer offers to mention: the members, again whenever they change.
  const { data: mentionable, reload: reloadMentionable } = useLoad(
    useCallback(() => api.listChatMentionable(id), [id]),
  );
  const reload = useCallback(
    () => Promise.all([reloadChat(), reloadMentionable()]),
    [reloadChat, reloadMentionable],
  );
  const [actionError, setActionError] = useState<string>();
  // The composer's unsent text, per chat. Held here because the room unmounts while the Members
  // tab is open; keyed so one chat's draft never shows in another.
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const setDraft = useCallback((draft: string) => setDrafts((all) => ({ ...all, [id]: draft })), [id]);
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
      navigate("/chats");
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
      await reloadList();
      // A private chat is gone for them once they've left; a public one is still there to read.
      if (chat!.visibility === "private") navigate("/chats");
      else await reload();
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  async function onAdd(kind: ChatTargetKind, targetId: number) {
    try {
      await api.addChatMember(id, { kind, id: targetId });
      await Promise.all([reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    }
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
      title={
        <span className="flex items-center gap-2">
          {chat.name}
          <Badge variant="secondary">{VISIBILITY[chat.visibility].label}</Badge>
        </span>
      }
      subtitle={
        <>
          {chat.description && <p className="text-foreground whitespace-pre-wrap">{chat.description}</p>}
          <p>
            {plural(chat.member_count, "member")} · Last active {formatDate(chat.updated_at)}
          </p>
        </>
      }
      actions={
        chat.is_owner ? (
          <>
            <Button asChild variant="outline" size="sm">
              <Link to="edit">
                <Pencil />
                Edit
              </Link>
            </Button>
            <Button variant="outline" size="sm" onClick={onDelete}>
              <Trash2 />
              Delete
            </Button>
          </>
        ) : chat.is_member ? (
          <Button variant="outline" size="sm" onClick={onLeave}>
            <LogOut />
            Leave
          </Button>
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
                canPost={chat.is_member}
                mentionable={mentionable}
                onPosted={reloadList}
                onRead={chat.is_member ? onRead : undefined}
                draft={drafts[chat.id] ?? ""}
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
                onAdd={chat.is_owner ? onAdd : undefined}
                onRemove={chat.is_owner ? onRemove : undefined}
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
  /** Set for the owner, who can add their own agents and workflows. */
  onAdd?: (kind: ChatTargetKind, id: number) => Promise<void>;
  /** Set for the owner, who can remove any member but themselves. */
  onRemove?: (member: ChatMember) => void;
}) {
  const groups: { title: string; members: ChatMember[]; empty: string; add?: ChatTargetKind }[] = [
    { title: "Users", members: chat.members.users, empty: "No users." },
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
                    avatarUrl={member.avatar_url}
                    // The name is beside it; an agent's or workflow's icon says which it is.
                    alt={member.kind === "user" ? "" : MEMBER_KIND[member.kind].label}
                  />
                  <span className="flex min-w-0 flex-1 items-baseline gap-1.5 truncate">
                    <span>{member.name}</span>
                    {member.login && member.login !== member.name && (
                      <span className="text-muted-foreground text-xs">@{member.login}</span>
                    )}
                  </span>
                  {member.role === "owner" && <Badge variant="secondary">owner</Badge>}
                  {onRemove && member.role !== "owner" && (
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

const ADDABLE = {
  agent: { list: api.listAgents, a: "an agent" },
  workflow: { list: api.listWorkflows, a: "a workflow" },
};

/** The owner's picker for adding one of their agents or workflows that isn't a member yet. */
function AddMember({
  kind,
  members,
  onAdd,
}: {
  kind: ChatTargetKind;
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
        Add {kind}
      </Button>
    </form>
  );
}

const emptyChat: ChatInput = { name: "", description: "", visibility: "private" };

export function ChatForm() {
  const params = useParams();
  const id = params.chatId ? Number(params.chatId) : undefined;
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const [form, setForm] = useState<ChatInput>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(undefined);
    if (id === undefined) setForm(emptyChat);
    else
      api.getChat(id).then(
        ({ name, description, visibility }) => setForm({ name, description, visibility }),
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
      navigate(`/chats/${saved.id}`);
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
        <Field label="Name" htmlFor="chat-name" hint="Unique among chats.">
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
        <Field label="Visibility" htmlFor="chat-visibility" hint={VISIBILITY[form.visibility].hint}>
          <select
            id="chat-visibility"
            className={selectClass}
            value={form.visibility}
            onChange={(e) => set({ visibility: e.target.value as ChatVisibility })}
          >
            {(Object.keys(VISIBILITY) as ChatVisibility[]).map((v) => (
              <option key={v} value={v}>
                {VISIBILITY[v].label}
              </option>
            ))}
          </select>
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
