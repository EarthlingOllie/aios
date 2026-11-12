import type { Connector, ScopeItem } from "@/lib/api";

/** Connectors: connections to external systems (the API's `aios.connectors`). */

/** What an admin must accept before syncing something restricted at its source (the API's own
 * 422 says the same). */
export const RESTRICTED_WARNING =
  "Some of what you picked is restricted at its source. Once it's synced, everyone in the organisation will be able to search it.";

/** The picked items that are restricted at their source, among those `known` (options or the
 * saved scope). */
export function restrictedPicked(
  known: ScopeItem[],
  picked: string[],
): ScopeItem[] {
  const chosen = new Set(picked);
  return known.filter((item) => item.restricted && chosen.has(item.id));
}

/** Whether `picked` differs from the saved scope. */
export function scopeChanged(saved: ScopeItem[], picked: string[]): boolean {
  const was = new Set(saved.map((item) => item.id));
  return was.size !== new Set(picked).size || picked.some((id) => !was.has(id));
}

/** How the connector stands, as a badge: whether it needs an admin's attention. */
export function connectorState(
  connector: Pick<Connector, "status" | "unavailable">,
): {
  label: string;
  attention: boolean;
} {
  if (connector.unavailable) return { label: "Unavailable", attention: true };
  if (connector.status === "needs_reauth")
    return { label: "Reconnect", attention: true };
  if (connector.status === "removing")
    return { label: "Being removed", attention: false };
  return { label: "Connected", attention: false };
}
