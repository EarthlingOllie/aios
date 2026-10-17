import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { Bot, Lock, Pencil, Trash2, UserMinus, Workflow } from "lucide-react";

import {
  DetailPage,
  ErrorBanner,
  Field,
  formatDate,
  MasterDetail,
  Placeholder,
  Section,
  selectClass,
  useLoad,
  type ListContext,
} from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  describeError,
  type Chat,
  type ChatInput,
  type ChatMember,
  type ChatVisibility,
} from "@/lib/api";
import { plural } from "@/lib/utils";

const VISIBILITY: Record<ChatVisibility, { label: string; hint: string }> = {
  public: { label: "Public", hint: "Anyone signed in can see it." },
  private: { label: "Private", hint: "Only its members can see it." },
};

export function ChatsSection() {
  const { data, error, reload } = useLoad(api.listChats);
  return (
    <MasterDetail
      title="Chats"
      basePath="/chats"
      items={data}
      error={error}
      empty="No chats yet. A chat is a group conversation; create one and add members."
      reloadList={reload}
      renderItem={(chat) => (
        <>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{chat.name}</span>
            {chat.visibility === "private" && (
              <Lock className="text-muted-foreground size-3.5 shrink-0" role="img" aria-label="Private" />
            )}
            <span className="text-muted-foreground shrink-0 text-xs">
              {plural(chat.member_count, "member")}
            </span>
          </div>
          {chat.description && (
            <div className="text-muted-foreground truncate text-xs">{chat.description}</div>
          )}
        </>
      )}
    />
  );
}

export function ChatsIndex() {
  return <Placeholder>Select a chat, or create a new one.</Placeholder>;
}

export function ChatDetail() {
  const id = Number(useParams().chatId);
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const { data: chat, error, reload } = useLoad(useCallback(() => api.getChat(id), [id]));
  const [actionError, setActionError] = useState<string>();

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
        chat.is_owner && (
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
        )
      }
    >
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />
      <Section title="Members" label="Members">
        <MembersPanel chat={chat} onRemove={chat.is_owner ? onRemove : undefined} />
      </Section>
    </DetailPage>
  );
}

function MembersPanel({
  chat,
  onRemove,
}: {
  chat: Chat;
  /** Set for the owner, who can remove any member but themselves. */
  onRemove?: (member: ChatMember) => void;
}) {
  const groups: { title: string; members: ChatMember[]; empty: string }[] = [
    { title: "Users", members: chat.members.users, empty: "No users." },
    { title: "Agents", members: chat.members.agents, empty: "No agents yet." },
    { title: "Workflows", members: chat.members.workflows, empty: "No workflows yet." },
  ];
  return (
    <div className="flex flex-col gap-4">
      {groups.map(({ title, members, empty }) => (
        <div key={title} className="flex flex-col gap-1.5">
          <h4 className="text-sm font-medium">{title}</h4>
          {members.length === 0 ? (
            <p className="text-muted-foreground text-sm">{empty}</p>
          ) : (
            <ul className="divide-y rounded-md border">
              {members.map((member) => (
                <li key={member.id} className="flex items-center gap-2 px-3 py-2 text-sm">
                  <MemberAvatar member={member} />
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
        </div>
      ))}
    </div>
  );
}

function MemberAvatar({ member }: { member: ChatMember }) {
  if (member.kind === "agent") return <Bot className="text-muted-foreground size-5" />;
  if (member.kind === "workflow") return <Workflow className="text-muted-foreground size-5" />;
  if (member.avatar_url) return <img src={member.avatar_url} alt="" className="size-5 rounded-full" />;
  return (
    <span className="bg-muted flex size-5 items-center justify-center rounded-full text-[10px] font-medium uppercase">
      {member.name.slice(0, 1)}
    </span>
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
