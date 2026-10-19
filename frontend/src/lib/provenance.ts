import type { Provenance, User } from "@/lib/api";

/** A member as a line of text names them: "you", their name, or else their login. */
export function personName(user: User, me?: User | null): string {
  return user.id === me?.id ? "you" : (user.name ?? user.login);
}

/**
 * Who made something of the organisation's and who last changed it, in a line: "Created by Sam
 * · last changed by Alex", or only who made it when that's who last changed it too. Null when
 * neither is known (made before organisations, or their accounts are deleted).
 */
export function describeProvenance(p: Provenance, me?: User | null): string | null {
  const created = p.created_by && `Created by ${personName(p.created_by, me)}`;
  if (!p.updated_by || p.updated_by.id === p.created_by?.id) return created;
  const changer = personName(p.updated_by, me);
  return created ? `${created} · last changed by ${changer}` : `Last changed by ${changer}`;
}
