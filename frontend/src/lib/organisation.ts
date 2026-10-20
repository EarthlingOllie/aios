import { ApiError, type ContentKind, type NotEmpty, type Organisation, type User } from "@/lib/api";

/** What the Organisation page says under the name: whose organisation it is and who made it. */
export function describeOrganisation(org: Organisation, me: Pick<User, "id"> | null): string {
  const creator = org.creator ? (org.creator.id === me?.id ? "you" : org.creator.name) : null;
  if (org.personal) {
    const whose = org.is_creator ? "Your personal organisation" : "A personal organisation";
    return creator ? `${whose}, created by ${creator}.` : `${whose}.`;
  }
  return creator ? `Created by ${creator}.` : "Its creator's account has been deleted.";
}

/** Why the signed-in user can't leave the organisation, as the API refuses it, or null if they
 * can: the last member can't (nobody would see what's in it), nor can the creator while others
 * remain (nobody else could invite). */
export function leaveRefusal(org: Organisation): string | null {
  if (org.members.length <= 1) {
    return "You're its only member, so you can't leave: an organisation can't be left empty.";
  }
  if (org.is_creator) {
    return "You created it, and only its creator can invite people, so you can't leave while others are still members.";
  }
  return null;
}

/** What an invitation link is called on the page: its token is never shown again, so it's named
 * by the start of its id, in the list of links and beside each member who came through it. */
export function linkName(id: string): string {
  return `link ${id.slice(0, 8)}`;
}

/** Where each kind of thing in an organisation is deleted, and what one is called. */
const CONTENT_PAGES: Record<ContentKind, { noun: string; to: string }> = {
  agent: { noun: "agent", to: "/agents" },
  workflow: { noun: "workflow", to: "/workflows" },
  skill: { noun: "skill", to: "/skills" },
  chat: { noun: "chat", to: "/chats" },
  schedule: { noun: "schedule", to: "/scheduler" },
  document: { noun: "document", to: "/data" },
  table: { noun: "data table", to: "/data" },
};

/** A link to where `count` things of a kind can be deleted, e.g. "2 agents" to the Agents page. */
export function contentLink(kind: ContentKind, count: number): { text: string; to: string } {
  const { noun, to } = CONTENT_PAGES[kind];
  return { text: `${count} ${noun}${count === 1 ? "" : "s"}`, to };
}

/** The refusal when accepting an invitation fails because the user's organisation has things in
 * it (a 409 whose detail says what), or null for any other failure. */
export function notEmpty(err: unknown): NotEmpty | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  try {
    const detail = JSON.parse(err.message)?.detail;
    if (typeof detail?.message === "string" && Array.isArray(detail.contents)) return detail;
  } catch {
    // not JSON
  }
  return null;
}
