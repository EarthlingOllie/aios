import type { Organisation, User } from "@/lib/api";

/** What the Organisation page says under the name: whose organisation it is and who made it. */
export function describeOrganisation(org: Organisation, me: User | null): string {
  const creator = org.creator
    ? org.creator.id === me?.id
      ? "you"
      : (org.creator.name ?? org.creator.login)
    : null;
  if (org.personal) {
    const whose = org.is_creator ? "Your personal organisation" : "A personal organisation";
    return creator ? `${whose}, created by ${creator}.` : `${whose}.`;
  }
  return creator ? `Created by ${creator}.` : "Its creator's account has been deleted.";
}
