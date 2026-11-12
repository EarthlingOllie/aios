/**
 * The Insights page's view, kept in the URL so a reload or a shared link shows the same thing:
 * a range of days (`?range=30d`) and a grouping (`?group=agent`); and how its numbers read.
 */

export const RANGES = [
  { id: "7d", label: "Last 7 days", days: 7 },
  { id: "30d", label: "Last 30 days", days: 30 },
  { id: "90d", label: "Last 90 days", days: 90 },
  { id: "365d", label: "Last 12 months", days: 365 },
] as const;

export type InsightsRange = (typeof RANGES)[number]["id"];

export const GROUPS = [
  { id: "agent", label: "Agent" },
  { id: "workflow", label: "Workflow" },
  { id: "cause", label: "Cause" },
  { id: "person", label: "Person" },
] as const;

export type InsightsGroup = (typeof GROUPS)[number]["id"];

export const DEFAULT_RANGE: InsightsRange = "30d";
export const DEFAULT_GROUP: InsightsGroup = "agent";

export type InsightsView = { range: InsightsRange; group: InsightsGroup };

/** The view a URL's query asks for; anything unknown falls back to the default. */
export function viewOf(params: URLSearchParams): InsightsView {
  const range = params.get("range");
  const group = params.get("group");
  return {
    range: RANGES.some((r) => r.id === range) ? (range as InsightsRange) : DEFAULT_RANGE,
    group: GROUPS.some((g) => g.id === group) ? (group as InsightsGroup) : DEFAULT_GROUP,
  };
}

/** The query that keeps `view` in the URL, leaving out what's the default. */
export function paramsOf(view: InsightsView): URLSearchParams {
  const params = new URLSearchParams();
  if (view.range !== DEFAULT_RANGE) params.set("range", view.range);
  if (view.group !== DEFAULT_GROUP) params.set("group", view.group);
  return params;
}

const day = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** The days a range covers, as YYYY-MM-DD (UTC, as the API's days are): today and the days
 * before it. */
export function rangeDays(range: InsightsRange, now = Date.now()): { start: string; end: string } {
  const days = RANGES.find((r) => r.id === range)!.days;
  return { start: day(now - (days - 1) * 86_400_000), end: day(now) };
}

/** Every day from `start` to `end`, both included. */
export function daysBetween(start: string, end: string): string[] {
  const out: string[] = [];
  for (let t = Date.parse(`${start}T00:00:00Z`); t <= Date.parse(`${end}T00:00:00Z`); t += 86_400_000) {
    out.push(day(t));
  }
  return out;
}

/** What share of the finished ones succeeded, as "75%"; "–" when none has finished. */
export function successRate(succeeded: number, failed: number): string {
  const finished = succeeded + failed;
  return finished === 0 ? "–" : `${Math.round((succeeded / finished) * 100)}%`;
}

/** A duration for people: "850 ms", "42 s", "3 min 5 s", "2 h 10 min". */
export function formatSeconds(seconds: number | null): string {
  if (seconds === null) return "–";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    const rest = Math.round(seconds - minutes * 60);
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes - hours * 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

const CAUSES: Record<string, string> = {
  mention: "Mentions",
  schedule: "Schedules",
  manual: "Started by hand",
  event: "Events",
};

/** A group's row name: its name, or what the key is, or what having none means. */
export function groupName(group: InsightsGroup, key: number | string | null, name: string | null): string {
  if (group === "cause") return key === null ? "No cause recorded" : (CAUSES[String(key)] ?? String(key));
  if (name) return name;
  if (key === null) {
    if (group === "person") return "No one (schedules and events)";
    return group === "workflow" ? "Not in a workflow" : "No agent";
  }
  return `#${key}`;
}

/** A count of tokens or calls, or "Not tracked" when none of the turns it covers kept them. */
export function tracked(value: number | null): string {
  return value === null ? "Not tracked" : Math.round(value).toLocaleString("en-GB");
}
