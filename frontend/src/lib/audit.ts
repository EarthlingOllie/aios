import type { AuditEntry } from "@/lib/api";

/** Who did it: the person, the agent in its run, or the platform itself. */
export function actorName(entry: AuditEntry): string {
  const { actor } = entry;
  if (actor.kind === "system") return "The platform";
  if (actor.kind === "agent") {
    const name = actor.agent?.name ?? (actor.agent ? `Agent #${actor.agent.id}` : "An agent");
    return actor.run_id !== null ? `${name} (run #${actor.run_id})` : name;
  }
  return actor.user?.name ?? "A deleted user";
}

/** What it was done to: its kind, and its name where the entry has one, else its id. */
export function targetText(entry: AuditEntry): string {
  if (!entry.target_kind) return "";
  const name = typeof entry.details.name === "string" ? entry.details.name : entry.target_id;
  return name ? `${entry.target_kind} ${name}` : entry.target_kind;
}

/** The rest of the details, in a line: a change as "from → to", anything else as "key: value"
 * (the name is the target's already). */
export function detailsText(entry: AuditEntry): string {
  const { name: _name, from, to, ...rest } = entry.details;
  const parts: string[] = [];
  if (from !== undefined || to !== undefined) parts.push(`${show(from)} → ${show(to)}`);
  for (const [key, value] of Object.entries(rest)) parts.push(`${key}: ${show(value)}`);
  return parts.join(", ");
}

function show(value: unknown): string {
  if (value === null || value === undefined) return "none";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** The actions the Audit log tab suggests filtering by: families, then each action. */
export const AUDIT_ACTIONS = [
  "agent",
  "skill",
  "workflow",
  "schedule",
  "source",
  "approval",
  "approval_rule",
  "member",
  "invitation",
  "organisation",
  "settings",
  "data",
  "agent.created",
  "agent.updated",
  "agent.archived",
  "agent.unarchived",
  "approval.decided",
  "approval_rule.changed",
  "member.role_changed",
  "member.joined",
  "member.left",
  "invitation.created",
  "invitation.revoked",
  "organisation.renamed",
  "organisation.deletion_scheduled",
  "settings.changed",
  "data.written",
];
