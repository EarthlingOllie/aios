import type { DroppedObject, Removal } from "@/lib/api";

const UNITS: [ms: number, one: string][] = [
  [24 * 60 * 60 * 1000, "day"],
  [60 * 60 * 1000, "hour"],
  [60 * 1000, "minute"],
];

/**
 * How long until a dropped table or column is purged, in its largest whole unit. The purge
 * runs hourly, so one whose time has come (or nearly) goes within the hour.
 */
export function purgeIn(purgeAt: string, now: Date = new Date()): string {
  const left = new Date(purgeAt).getTime() - now.getTime();
  for (const [ms, one] of UNITS) {
    const n = Math.floor(left / ms);
    if (n >= 1) return `Purged in ${n} ${one}${n === 1 ? "" : "s"}`;
  }
  return "Purged within the hour";
}

function rows(n: number) {
  return `${n} row${n === 1 ? "" : "s"}`;
}

/** What one of a request's drops would remove, in a line for its card. */
export function removalText(removal: Removal): string {
  const what = removal.column
    ? `Column ${removal.column} of data.${removal.table}`
    : `Table data.${removal.table}`;
  if (removal.rows === null && !removal.estimated) return `${what}: not there any more`;
  // Counting took too long: Postgres's estimate, if it has one.
  const counted =
    removal.rows === null
      ? "too many rows to count quickly"
      : removal.estimated
        ? `about ${rows(removal.rows)}`
        : null;
  const columns = removal.column ? "" : `, with columns ${(removal.columns ?? []).join(", ")}`;
  if (counted) return `${what}: ${counted}${columns}`;
  if (removal.column) return `${what}: a value in ${removal.values ?? 0} of ${rows(removal.rows!)}`;
  return `${what}: ${rows(removal.rows!)}${columns}`;
}

/** Whether what the owner typed confirms a drop: exactly the names asked for. */
export function confirmed(typed: string, required: string): boolean {
  return typed.trim() === required;
}

/** A dropped table or column, by name. */
export function droppedLabel(item: Pick<DroppedObject, "kind" | "table" | "column">): string {
  return item.kind === "table"
    ? `Table data.${item.table}`
    : `Column ${item.column} of data.${item.table}`;
}
