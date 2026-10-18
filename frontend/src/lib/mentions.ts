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

/** The members whose name or login contains the query, ignoring case, still grouped by kind. */
export function filterMentionable(mentionable: Mentionables, query: string): Mentionables {
  const q = query.toLowerCase();
  const keep = (members: Mentionable[]) =>
    members.filter((m) => m.name.toLowerCase().includes(q) || (m.login ?? "").toLowerCase().includes(q));
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
