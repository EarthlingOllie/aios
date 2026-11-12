import type { ChatMemberKind, ChatMention, ChatMentionable, ChatMentionables, ChatMentionRef } from "@/lib/api";

export type Mentionable = ChatMentionable;
export type Mentionables = ChatMentionables;

/**
 * A mention in a message's markdown is a link from an `@`: `@[name](kind:id)`. The name is for
 * whoever reads the raw text; the kind and id say who it is, so an agent and a workflow with the
 * same name are never confused. The message's structured `mentions` are what the chip shows.
 */
export function mentionToken({ kind, id, name }: Mentionable): string {
  return `@[${name.replace(/[\\[\]]/g, "\\$&")}](${kind}:${id})`;
}

const KINDS = "user|agent|workflow";
const HREF = new RegExp(`^(${KINDS}):([1-9]\\d*)$`);
const TOKEN = new RegExp(`@\\[((?:\\\\.|[^\\\\\\]])*)\\]\\((${KINDS}):([1-9]\\d*)\\)`, "g");

/** Who a mention's link (`kind:id`) points at, or null if it's any other link. */
export function mentionAt(href: string): ChatMentionRef | null {
  const match = HREF.exec(href);
  return match ? { kind: match[1] as ChatMemberKind, id: Number(match[2]) } : null;
}

/** The members a body's tokens mention, each once and in order, leaving out anyone not mentionable. */
export function mentionsIn(body: string, mentionable: Mentionables): ChatMentionRef[] {
  const members = new Set(
    [...mentionable.users, ...mentionable.agents, ...mentionable.workflows].map((m) => `${m.kind}:${m.id}`),
  );
  const found = new Map<string, ChatMentionRef>();
  for (const [, , kind, id] of body.matchAll(TOKEN)) {
    const key = `${kind}:${id}`;
    if (members.has(key) && !found.has(key)) found.set(key, { kind: kind as ChatMemberKind, id: Number(id) });
  }
  return [...found.values()];
}

/** The `@` word being typed: where its `@` is, and what follows it up to the caret. */
export type MentionQuery = { start: number; query: string };

/**
 * The `@` word the caret is at the end of, or null: an `@` at the start or after a space, then
 * anything but spaces and brackets (so an email address or an inserted mention isn't one).
 */
export function mentionQuery(text: string, caret: number): MentionQuery | null {
  const match = /(?:^|\s)@([^\s@[\]()]*)$/.exec(text.slice(0, caret));
  if (!match) return null;
  return { start: caret - match[1].length - 1, query: match[1] };
}

/** The text with the `@` word replaced by the member's token and a space, and the caret after them. */
export function insertMention(text: string, at: MentionQuery, member: Mentionable) {
  const inserted = `${mentionToken(member)} `;
  const end = at.start + 1 + at.query.length;
  return { text: text.slice(0, at.start) + inserted + text.slice(end), caret: at.start + inserted.length };
}

/**
 * The members the query finds, ignoring case, still grouped by kind: users whose first or last
 * name contains it, and agents and workflows whose name does.
 */
export function filterMentionable(mentionable: Mentionables, query: string): Mentionables {
  const q = query.toLowerCase();
  const names = (m: Mentionable) => (m.kind === "user" ? [m.first_name ?? "", m.last_name ?? ""] : [m.name]);
  const keep = (members: Mentionable[]) =>
    members.filter((m) => names(m).some((name) => name.toLowerCase().includes(q)));
  return { users: keep(mentionable.users), agents: keep(mentionable.agents), workflows: keep(mentionable.workflows) };
}

/** As much of a markdown (mdast) node as `remarkMentions` reads. */
type MdNode = { type: string; value?: string; url?: string; children?: MdNode[]; data?: object };

const plainText = (node: MdNode): string => node.value ?? (node.children ?? []).map(plainText).join("");

/**
 * A remark plugin that turns each mention in a message (an `@` then a `kind:id` link) into a
 * `<span data-mention="kind:id">` holding the member's name as the message stored it, for the
 * renderer to show as a chip. A token for anyone the message doesn't mention (typed by hand, say)
 * stays plain text: `@name`.
 */
export function remarkMentions(mentions: ChatMention[]) {
  const named = new Map(mentions.map((m) => [`${m.kind}:${m.id}`, m.name]));
  const visit = (node: MdNode) => {
    const children = node.children;
    if (!children) return;
    children.forEach((child, i) => {
      const before = children[i - 1];
      const ref = child.type === "link" && child.url !== undefined ? mentionAt(child.url) : null;
      if (!ref || before?.type !== "text" || !before.value?.endsWith("@")) return visit(child);
      const key = `${ref.kind}:${ref.id}`;
      const name = named.get(key);
      if (name === undefined) {
        children[i] = { type: "text", value: plainText(child) };
        return;
      }
      before.value = before.value.slice(0, -1);
      children[i] = {
        type: "mention",
        data: { hName: "span", hProperties: { dataMention: key }, hChildren: [{ type: "text", value: name }] },
      };
    });
  };
  return () => (tree: MdNode) => visit(tree);
}

/**
 * Who the composer offers to mention: the chat's members, then the organisation's agents and
 * workflows the chat doesn't include. Posting a mention of one of those is refused, naming it,
 * and the room offers to add it to the chat (`postRefusal`). Just the chat's own until the
 * organisation's have loaded.
 */
export function composerMentionables(
  chat: Mentionables | undefined,
  organisation: (ChatMentionRef & { name: string })[] | undefined,
): Mentionables | undefined {
  if (!chat || !organisation) return chat;
  const included = new Set([...chat.agents, ...chat.workflows].map((m) => `${m.kind}:${m.id}`));
  const more = organisation
    .filter((t) => t.kind !== "user" && !included.has(`${t.kind}:${t.id}`))
    .map((t) => ({ kind: t.kind, id: t.id, name: t.name, first_name: null, last_name: null }));
  return {
    users: chat.users,
    agents: [...chat.agents, ...more.filter((m) => m.kind === "agent")],
    workflows: [...chat.workflows, ...more.filter((m) => m.kind === "workflow")],
  };
}
