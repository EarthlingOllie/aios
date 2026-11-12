import type { Role } from "@/lib/api";

/**
 * What a member does, as the API's permission table (`aios.permissions`) names it: the screens
 * hide or disable what the signed-in member's role can't do, and say why (`refusal`). The API
 * enforces the same table, so this is only how the screen shows it.
 */
export type Action =
  | "read"
  | "post"
  | "build"
  | "documents"
  | "decide"
  | "restore"
  | "tables"
  | "sources"
  | "run_organisation";

const EVERYONE: Role[] = ["admin", "editor", "participant"];
const BUILDERS: Role[] = ["admin", "editor"];
const ADMINS: Role[] = ["admin"];

const TABLE: Record<Action, { roles: Role[]; does: string }> = {
  read: { roles: EVERYONE, does: "read and search what's in the organisation" },
  post: { roles: EVERYONE, does: "chat, mention and start runs" },
  build: {
    roles: BUILDERS,
    does: "create, edit, archive or unarchive agents, skills, workflows and schedules",
  },
  documents: { roles: BUILDERS, does: "upload or delete documents" },
  decide: { roles: BUILDERS, does: "approve or reject schema change requests" },
  restore: { roles: ADMINS, does: "restore dropped tables and columns" },
  tables: { roles: BUILDERS, does: "share, unshare or transfer data tables" },
  sources: { roles: ADMINS, does: "add, change, sync, pause or remove sources" },
  run_organisation: {
    roles: ADMINS,
    does: "change the organisation's roles, invitations, name or settings, or delete it",
  },
};

/** Whether a member with `role` may take `action`; one whose role isn't known yet may only do
 * what everyone may. */
export function can(role: Role | undefined | null, action: Action): boolean {
  return TABLE[action].roles.includes(role ?? "participant");
}

/** "Admin", "Editor" or "Participant". */
export function roleLabel(role: Role): string {
  return role[0].toUpperCase() + role.slice(1);
}

/** The role with its article: "an admin", "an editor", "a participant". */
export function aRole(role: Role): string {
  return role === "participant" ? "a participant" : `an ${role}`;
}

/** Why a member with `role` can't take `action` (naming it, and whom to ask), or null if they
 * can. */
export function refusal(role: Role | undefined | null, action: Action): string | null {
  if (can(role, action)) return null;
  // The least role that may first: "an editor or an admin".
  const who = [...TABLE[action].roles]
    .reverse()
    .map(aRole)
    .join(" or ");
  const plural = role ? `${roleLabel(role)}s` : "You";
  return `${plural} can't ${TABLE[action].does}. Ask ${who}.`;
}

/** What each role may do, for the Members tab's role picker. */
export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  admin: "Runs the organisation: members, roles, invitations, its name, settings and deletion.",
  editor: "Builds agents, skills, workflows and schedules, and uploads and deletes documents.",
  participant: "Chats, starts runs, and views, searches and downloads documents.",
};
