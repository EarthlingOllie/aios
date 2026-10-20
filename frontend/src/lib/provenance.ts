import type { Provenance, User } from "@/lib/api";

/** Whom `me` is, for saying "you": the signed-in user, or anyone with an id. */
type Self = Pick<User, "id"> | null | undefined;

/** A member as a line of text names them: "you", or their name. */
export function personName(user: User, me?: Self): string {
  return user.id === me?.id ? "you" : user.name;
}

/**
 * Who made something of the organisation's and who last changed it, in a line: "Created by Sam
 * · last changed by Alex", or only who made it when that's who last changed it too. Null when
 * neither is known (made before organisations, or their accounts are deleted).
 */
export function describeProvenance(p: Provenance, me?: Self): string | null {
  const created = p.created_by && `Created by ${personName(p.created_by, me)}`;
  if (!p.updated_by || p.updated_by.id === p.created_by?.id) return created;
  const changer = personName(p.updated_by, me);
  return created ? `${created} · last changed by ${changer}` : `Last changed by ${changer}`;
}

/** What says who started a run: the member, or else the schedule that did. */
export type Started = { created_by: User | null; trigger_id: number | null };

/**
 * Who started a run, in a line: "Started by Sam", or "Scheduled" for a schedule's run, which no
 * member started. Null when neither is known (a run from before runs recorded it, or whose
 * member's account or schedule has since been deleted).
 */
export function describeStarter(run: Started, me?: Self): string | null {
  if (run.created_by) return `Started by ${personName(run.created_by, me)}`;
  return run.trigger_id !== null ? "Scheduled" : null;
}
