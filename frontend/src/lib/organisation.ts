import { type Organisation, type User } from "@/lib/api";

/** What the Organisation page says under the name: who made it. */
export function describeOrganisation(org: Organisation, me: Pick<User, "id"> | null): string {
  const creator = org.creator ? (org.creator.id === me?.id ? "you" : org.creator.name) : null;
  return creator ? `Created by ${creator}.` : "Its creator's account has been deleted.";
}

/** Whether what the creator typed confirms deleting the organisation: its name, exactly (as the
 * API checks it, spaces around it aside). */
export function confirmsDeletion(typed: string, name: string): boolean {
  return typed.trim() === name;
}

/** Why the signed-in user can't leave the organisation, as the API refuses it, or null if they
 * can: the last member can't (nobody would see what's in it: they should delete it instead), nor
 * can the creator while others remain (nobody else could invite). */
export function leaveRefusal(org: Organisation): string | null {
  if (org.members.length <= 1) {
    return "You're its only member, so nobody would be left to see what's in it. Delete the organisation instead.";
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
