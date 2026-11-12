import type { Archived, ChatAction, User } from "@/lib/api";
import { mentionToken } from "@/lib/mentions";
import { personName } from "@/lib/provenance";

/** Whether the reader may take `action` on the chat, as the API's chat rules say (`actions`): the
 * screens show what they may do and hide the rest. */
export function can(chat: { actions: ChatAction[] }, action: ChatAction): boolean {
  return chat.actions.includes(action);
}

/** The banner on an archived chat: who archived it, and that it's read-only but still read and
 * searched. Null while it isn't archived. */
export function archivedNotice(chat: Archived, me?: Pick<User, "id"> | null): string | null {
  if (!chat.archived_at) return null;
  const by = chat.archived_by ? ` by ${personName(chat.archived_by, me)}` : "";
  return `This chat was archived${by}. It's read-only: nobody can post in it, join it or change it, but it's still here to read and search.`;
}

/** The short notice every chat shows: it's open to the whole organisation (`orgName`'s, once
 * known), not a private conversation, even one made with **Chat with this agent**. */
export function openNotice(orgName: string | undefined): string {
  return `Everyone in ${orgName ?? "the organisation"} can see this chat and read it, joined or not.`;
}

/** What the composer of a chat made with **Chat with this agent** starts with: a mention of
 * that agent, so the first message asks it. */
export function agentChatDraft(agent: { id: number; name: string }): string {
  return `${mentionToken({ kind: "agent", id: agent.id, name: agent.name, first_name: null, last_name: null })} `;
}
