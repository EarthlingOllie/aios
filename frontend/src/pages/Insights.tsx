import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { Loader2 } from "lucide-react";

import { ErrorBanner, Section, formatDate, selectClass, useLoad } from "@/components/crud";
import {
  api,
  describeError,
  type InsightsActivity,
  type InsightsApprovals,
  type InsightsChats,
  type InsightsSeries,
  type InsightsSources,
  type InsightsStorage,
  type InsightsUsage,
  type InsightsWorkflows,
} from "@/lib/api";
import { useCurrentOrganisation } from "@/lib/current-user";
import { formatBytes } from "@/lib/format-bytes";
import {
  GROUPS,
  RANGES,
  daysBetween,
  formatSeconds,
  groupName,
  paramsOf,
  rangeDays,
  successRate,
  tracked,
  viewOf,
  type InsightsGroup,
  type InsightsView,
} from "@/lib/insights";
import { formatCost } from "@/lib/limits";
import { sourceHealthLabel } from "@/lib/sources";
import { cn } from "@/lib/utils";

type Range = { start: string; end: string };

/**
 * Insights: how the organisation's agents and workflows are doing, for every member. The range
 * and grouping are kept in the URL (`?range=7d&group=cause`), so a reload or a shared link shows
 * the same view. Usage by person is admins' only unless an admin shows it to everyone, which
 * they switch here.
 */
export function InsightsPage() {
  const [params, setParams] = useSearchParams();
  const view = viewOf(params);
  // Today's range, fixed while the page is open: panels aren't re-read as the clock moves on.
  const range = useMemo(() => rangeDays(view.range), [view.range]);
  const setView = (next: Partial<InsightsView>) =>
    setParams(paramsOf({ ...view, ...next }), { replace: true });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-5xl flex-col gap-8 px-4 py-6 md:px-8">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">Insights</h1>
            <p className="text-muted-foreground mt-1 text-sm">
              How your organisation's agents and workflows are doing.
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-3 text-sm">
            <label className="flex flex-col gap-1">
              <span className="font-medium">Range</span>
              <select
                aria-label="Range"
                className={selectClass + " w-auto"}
                value={view.range}
                onChange={(e) => setView({ range: e.target.value as InsightsView["range"] })}
              >
                {RANGES.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="font-medium">Group by</span>
              <select
                aria-label="Group by"
                className={selectClass + " w-auto"}
                value={view.group}
                onChange={(e) => setView({ group: e.target.value as InsightsGroup })}
              >
                {GROUPS.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </header>
        <PeopleSetting />
        <ActivityPanel range={range} group={view.group} />
        <UsagePanel range={range} group={view.group} />
        <WorkflowsPanel range={range} />
        <ApprovalsPanel range={range} />
        <ChatsPanel range={range} />
        <StoragePanel range={range} />
        <SourcesPanel />
      </div>
    </div>
  );
}

/** A panel's data, read again when its range or grouping changes. */
function usePanel<T>(panel: string, range: Range, group?: InsightsGroup) {
  const load = useCallback(
    () => api.insights<T>(panel, range, group),
    [panel, range, group],
  );
  return useLoad(load);
}

function Panel({
  title,
  description,
  error,
  loading,
  children,
}: {
  title: string;
  description?: ReactNode;
  error?: string;
  loading: boolean;
  children?: ReactNode;
}) {
  return (
    <Section title={title} label={title}>
      {description && <p className="text-muted-foreground text-sm">{description}</p>}
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : loading ? (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      ) : (
        children
      )}
    </Section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground text-sm">{children}</p>;
}

function Table({ head, children, label }: { head: string[]; children: ReactNode; label: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm" aria-label={label}>
        <thead>
          <tr className="text-muted-foreground border-b text-left text-xs">
            {head.map((h, i) => (
              <th key={h} className={cn("py-1.5 pr-3 font-medium", i > 0 && "text-right")}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function Cell({ children, first = false }: { children: ReactNode; first?: boolean }) {
  return <td className={cn("py-1.5 pr-3 tabular-nums", !first && "text-right")}>{children}</td>;
}

const count = (n: number) => n.toLocaleString("en-GB");

// --- Usage by person ------------------------------------------------------------------------------

/** Whether usage by person is shown to everyone: admins switch it; everyone else is told. */
function PeopleSetting() {
  const membership = useCurrentOrganisation();
  const { data: org, reload } = useLoad(api.getOrganisation);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  if (!org || !membership) return null;
  const admin = membership.role === "admin";
  if (!admin) {
    return org.insights_people_visible ? null : (
      <p className="text-muted-foreground text-sm">Usage by person is shown to admins only.</p>
    );
  }

  async function toggle(visible: boolean) {
    setSaving(true);
    setError(undefined);
    try {
      await api.setInsightsPeopleVisible(visible);
      await reload();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-1 text-sm">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={org.insights_people_visible}
          disabled={saving}
          onChange={(e) => toggle(e.target.checked)}
        />
        Show usage by person to everyone, not only admins
      </label>
    </div>
  );
}

// --- Activity ------------------------------------------------------------------------------------

/** The days' totals over every series. */
function totalsByDay(series: InsightsSeries[], days: string[]) {
  const byDay = new Map(days.map((d) => [d, { succeeded: 0, failed: 0, other: 0 }]));
  for (const s of series) {
    for (const d of s.days) {
      const t = byDay.get(d.day);
      if (!t) continue;
      t.succeeded += d.succeeded;
      t.failed += d.failed;
      t.other += d.total - d.succeeded - d.failed;
    }
  }
  return days.map((day) => ({ day, ...byDay.get(day)! }));
}

/** Bars by day: succeeded, failed and the rest (still going, or not tracked), stacked. */
function DayBars({ series, range, label }: { series: InsightsSeries[]; range: Range; label: string }) {
  const days = daysBetween(range.start, range.end);
  const totals = totalsByDay(series, days);
  const max = Math.max(1, ...totals.map((t) => t.succeeded + t.failed + t.other));
  const width = 100 / days.length;
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox="0 0 100 40"
      preserveAspectRatio="none"
      className="h-28 w-full"
    >
      {totals.map((t, i) => {
        const parts = [
          { n: t.succeeded, className: "fill-primary" },
          { n: t.failed, className: "fill-destructive" },
          { n: t.other, className: "fill-muted-foreground/40" },
        ];
        let y = 40;
        return (
          <g key={t.day}>
            <title>{`${t.day}: ${t.succeeded} succeeded, ${t.failed} failed, ${t.other} other`}</title>
            {parts.map((p, j) => {
              const h = (p.n / max) * 38;
              y -= h;
              return (
                <rect
                  key={j}
                  x={i * width + width * 0.1}
                  y={y}
                  width={width * 0.8}
                  height={h}
                  className={p.className}
                />
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}

function seriesTotals(s: InsightsSeries) {
  return s.days.reduce(
    (t, d) => ({
      total: t.total + d.total,
      succeeded: t.succeeded + d.succeeded,
      failed: t.failed + d.failed,
      not_tracked: t.not_tracked + d.not_tracked,
    }),
    { total: 0, succeeded: 0, failed: 0, not_tracked: 0 },
  );
}

function SeriesTable({
  series,
  group,
  label,
  noun,
}: {
  series: InsightsSeries[];
  group: InsightsGroup;
  label: string;
  noun: string;
}) {
  if (series.length === 0) return <Empty>No {noun} in this range.</Empty>;
  const rows = series
    .map((s) => ({ s, t: seriesTotals(s) }))
    .sort((a, b) => b.t.total - a.t.total);
  return (
    <Table label={label} head={[GROUPS.find((g) => g.id === group)!.label, noun[0].toUpperCase() + noun.slice(1), "Succeeded", "Failed", "Success rate", "Not tracked"]}>
      {rows.map(({ s, t }) => (
        <tr key={String(s.key)} className="border-b last:border-0">
          <Cell first>{groupName(group, s.key, s.name)}</Cell>
          <Cell>{count(t.total)}</Cell>
          <Cell>{count(t.succeeded)}</Cell>
          <Cell>{count(t.failed)}</Cell>
          <Cell>{successRate(t.succeeded, t.failed)}</Cell>
          <Cell>{t.not_tracked ? count(t.not_tracked) : "–"}</Cell>
        </tr>
      ))}
    </Table>
  );
}

function ActivityPanel({ range, group }: { range: Range; group: InsightsGroup }) {
  const { data, error } = usePanel<InsightsActivity>("activity", range, group);
  return (
    <Panel
      title="Activity"
      description="Turns and workflow runs over time, and how many succeeded. Turns from before counts were kept are counted, as not tracked."
      error={error}
      loading={!data}
    >
      {data && (
        <div className="flex flex-col gap-4">
          <DayBars series={data.turns} range={range} label="Turns by day" />
          <SeriesTable series={data.turns} group={group} label="Turns" noun="turns" />
          <DayBars series={data.workflow_runs} range={range} label="Workflow runs by day" />
          <SeriesTable series={data.workflow_runs} group={group} label="Workflow runs" noun="runs" />
        </div>
      )}
    </Panel>
  );
}

// --- Usage ---------------------------------------------------------------------------------------

/** Horizontal bars, longest first: the most used agents or tools. */
function TopList({ items, label }: { items: { name: string; value: number }[]; label: string }) {
  if (items.length === 0) return <Empty>Nothing yet.</Empty>;
  const max = Math.max(...items.map((i) => i.value));
  return (
    <ol className="flex flex-col gap-1 text-sm" aria-label={label}>
      {items.map((i) => (
        <li key={i.name} className="flex items-center gap-2">
          <span className="w-40 truncate">{i.name}</span>
          <span className="bg-muted h-2 flex-1 overflow-hidden rounded-full">
            <span className="bg-primary block h-full" style={{ width: `${(i.value / max) * 100}%` }} />
          </span>
          <span className="w-16 text-right tabular-nums">{count(i.value)}</span>
        </li>
      ))}
    </ol>
  );
}

function UsagePanel({ range, group }: { range: Range; group: InsightsGroup }) {
  const { data, error } = usePanel<InsightsUsage>("usage", range, group);
  const notTracked = data?.rows.reduce((n, r) => n + r.not_tracked, 0) ?? 0;
  return (
    <Panel
      title="Usage and cost"
      description="Tokens, tool calls and estimated cost in pounds sterling. Costs are an estimate, not a bill."
      error={error}
      loading={!data}
    >
      {data && (
        <div className="flex flex-col gap-4">
          {data.rows.length === 0 ? (
            <Empty>No usage in this range.</Empty>
          ) : (
            <Table
              label="Usage"
              head={[GROUPS.find((g) => g.id === group)!.label, "Turns", "Input tokens", "Output tokens", "Tool calls", "Cost (estimate)"]}
            >
              {data.rows.map((r) => (
                <tr key={String(r.key)} className="border-b last:border-0">
                  <Cell first>{groupName(group, r.key, r.name)}</Cell>
                  <Cell>{count(r.turns)}</Cell>
                  <Cell>{tracked(r.input_tokens)}</Cell>
                  <Cell>{tracked(r.output_tokens)}</Cell>
                  <Cell>{tracked(r.tool_calls)}</Cell>
                  <Cell>{formatCost(r.cost)}</Cell>
                </tr>
              ))}
            </Table>
          )}
          {notTracked > 0 && (
            <p className="text-muted-foreground text-xs">
              {count(notTracked)} {notTracked === 1 ? "turn is" : "turns are"} from before counts were kept: counted,
              but their tokens and tool calls are not tracked.
            </p>
          )}
          <div className="grid gap-4 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <h4 className="text-sm font-medium">Most used agents</h4>
              <TopList label="Most used agents" items={data.top_agents.map((a) => ({ name: a.name, value: a.turns }))} />
            </div>
            <div className="flex flex-col gap-2">
              <h4 className="text-sm font-medium">Most used tools</h4>
              <TopList label="Most used tools" items={data.top_tools.map((t) => ({ name: t.tool, value: t.calls }))} />
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// --- Workflows -----------------------------------------------------------------------------------

function WorkflowsPanel({ range }: { range: Range }) {
  const { data, error } = usePanel<InsightsWorkflows>("workflows", range);
  return (
    <Panel
      title="Workflows"
      description="The slowest workflows and nodes, without the time they waited for approvals, and where runs fail."
      error={error}
      loading={!data}
    >
      {data && (
        <div className="flex flex-col gap-4">
          <h4 className="text-sm font-medium">Slowest workflows</h4>
          {data.slowest_workflows.length === 0 ? (
            <Empty>No finished runs in this range.</Empty>
          ) : (
            <Table label="Slowest workflows" head={["Workflow", "Runs", "Mean time", "Longest", "Waited for approvals"]}>
              {data.slowest_workflows.map((w) => (
                <tr key={w.workflow_id} className="border-b last:border-0">
                  <Cell first>
                    <Link to={`/workflows/${w.workflow_id}`} className="underline underline-offset-4">
                      {w.name}
                    </Link>
                  </Cell>
                  <Cell>{count(w.runs)}</Cell>
                  <Cell>{formatSeconds(w.mean_seconds)}</Cell>
                  <Cell>{formatSeconds(w.max_seconds)}</Cell>
                  <Cell>{formatSeconds(w.waited_seconds)}</Cell>
                </tr>
              ))}
            </Table>
          )}
          <h4 className="text-sm font-medium">Slowest nodes</h4>
          {data.slowest_nodes.length === 0 ? (
            <Empty>No finished nodes in this range.</Empty>
          ) : (
            <Table label="Slowest nodes" head={["Node", "Runs", "Mean time", "Longest"]}>
              {data.slowest_nodes.map((n) => (
                <tr key={`${n.workflow_id}/${n.node}`} className="border-b last:border-0">
                  <Cell first>
                    {n.name} · {n.node}
                  </Cell>
                  <Cell>{count(n.runs)}</Cell>
                  <Cell>{formatSeconds(n.mean_seconds)}</Cell>
                  <Cell>{formatSeconds(n.max_seconds)}</Cell>
                </tr>
              ))}
            </Table>
          )}
          <h4 className="text-sm font-medium">Where runs fail</h4>
          {data.failures.length === 0 ? (
            <Empty>No failures in this range.</Empty>
          ) : (
            <Table label="Where runs fail" head={["Node", "Failures", "Latest error"]}>
              {data.failures.map((f) => (
                <tr key={`${f.workflow_id}/${f.node}`} className="border-b last:border-0">
                  <Cell first>
                    {f.name} · {f.node}
                  </Cell>
                  <Cell>{count(f.failures)}</Cell>
                  <Cell>
                    <Link
                      to={`/workflows/${f.workflow_id}/runs/${f.latest_run_id}`}
                      className="underline underline-offset-4"
                      aria-label={`Latest error at ${f.name} · ${f.node}`}
                    >
                      {f.latest_at ? formatDate(f.latest_at) : "Latest run"}
                    </Link>
                  </Cell>
                </tr>
              ))}
            </Table>
          )}
        </div>
      )}
    </Panel>
  );
}

// --- Approvals -----------------------------------------------------------------------------------

const APPROVAL_KINDS: Record<string, string> = {
  schema_change: "Schema changes",
  workflow_step: "Workflow approval steps",
};

function ApprovalsPanel({ range }: { range: Range }) {
  const { data, error } = usePanel<InsightsApprovals>("approvals", range);
  return (
    <Panel
      title="Approvals"
      description="How approvals were decided and how long they waited. Expired approvals are counted apart: nobody decided them."
      error={error}
      loading={!data}
    >
      {data &&
        (data.kinds.length === 0 ? (
          <Empty>No approvals in this range.</Empty>
        ) : (
          <Table
            label="Approvals"
            head={["Kind", "Asked", "Approved", "Rejected", "Approval rate", "Expired", "Waiting", "Mean wait", "Longest wait"]}
          >
            {data.kinds.map((k) => (
              <tr key={k.kind} className="border-b last:border-0">
                <Cell first>{APPROVAL_KINDS[k.kind] ?? k.kind}</Cell>
                <Cell>{count(k.total)}</Cell>
                <Cell>{count(k.approved)}</Cell>
                <Cell>{count(k.rejected)}</Cell>
                <Cell>{successRate(k.approved, k.rejected)}</Cell>
                <Cell>{count(k.expired)}</Cell>
                <Cell>{count(k.pending)}</Cell>
                <Cell>{formatSeconds(k.mean_wait_seconds)}</Cell>
                <Cell>{formatSeconds(k.max_wait_seconds)}</Cell>
              </tr>
            ))}
          </Table>
        ))}
    </Panel>
  );
}

// --- Chats ---------------------------------------------------------------------------------------

function ChatsPanel({ range }: { range: Range }) {
  const { data, error } = usePanel<InsightsChats>("chats", range);
  return (
    <Panel
      title="Chat activity"
      description="Where your organisation works with its agents: messages, mentions and how the runs they started went."
      error={error}
      loading={!data}
    >
      {data &&
        (data.chats.length === 0 ? (
          <Empty>No chat activity in this range.</Empty>
        ) : (
          <Table
            label="Chat activity"
            head={["Chat", "Messages", "Agent replies", "Mentions", "Succeeded", "Failed", "Refused", "In progress"]}
          >
            {data.chats.map((c) => (
              <tr key={c.chat_id} className="border-b last:border-0">
                <Cell first>
                  <Link to={`/chats/${c.chat_id}`} className="underline underline-offset-4">
                    {c.name}
                  </Link>
                  {c.archived && <span className="text-muted-foreground"> (archived)</span>}
                </Cell>
                <Cell>{count(c.messages)}</Cell>
                <Cell>{count(c.agent_messages)}</Cell>
                <Cell>{count(c.mentions)}</Cell>
                <Cell>{count(c.succeeded)}</Cell>
                <Cell>{count(c.failed)}</Cell>
                <Cell>{count(c.refused)}</Cell>
                <Cell>{count(c.in_progress)}</Cell>
              </tr>
            ))}
          </Table>
        ))}
    </Panel>
  );
}

// --- Storage -------------------------------------------------------------------------------------

/** A line of samples' shares of a cap over the range. */
function StorageLine({
  points,
  label,
  range,
}: {
  points: { at: number; share: number }[];
  label: string;
  range: Range;
}) {
  const from = Date.parse(`${range.start}T00:00:00Z`);
  const to = Date.parse(`${range.end}T00:00:00Z`) + 86_400_000;
  const x = (t: number) => ((t - from) / (to - from)) * 100;
  const y = (share: number) => 40 - Math.min(1, share) * 38;
  return (
    <svg role="img" aria-label={label} viewBox="0 0 100 40" preserveAspectRatio="none" className="h-20 w-full">
      <line x1={0} x2={100} y1={y(1)} y2={y(1)} className="stroke-destructive" strokeDasharray="2 1" strokeWidth={0.3} />
      <polyline
        fill="none"
        className="stroke-primary"
        strokeWidth={0.6}
        vectorEffect="non-scaling-stroke"
        points={points.map((p) => `${x(p.at)},${y(p.share)}`).join(" ")}
      />
    </svg>
  );
}

function StoragePanel({ range }: { range: Range }) {
  const { data, error } = usePanel<InsightsStorage>("storage", range);
  return (
    <Panel title="Storage" description="What your data and documents use against their caps over time." error={error} loading={!data}>
      {data && (
        <div className="grid gap-4 md:grid-cols-2">
          {(
            [
              ["Data stored", "db_bytes", data.db_quota_bytes],
              ["Documents", "files_bytes", data.files_quota_bytes],
            ] as const
          ).map(([label, column, cap]) => (
            <div key={column} className="flex flex-col gap-1 text-sm">
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-medium">{label}</span>
                <span className="text-muted-foreground">
                  {data[column] === null ? "Not measured yet" : `${formatBytes(data[column]!)} of ${formatBytes(cap)}`}
                </span>
              </div>
              {data.samples.length === 0 ? (
                <Empty>No measurements in this range yet.</Empty>
              ) : (
                <StorageLine
                  label={`${label} over time`}
                  range={range}
                  points={data.samples.map((s) => ({ at: Date.parse(s.measured_at), share: s[column] / cap }))}
                />
              )}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

// --- Sources -------------------------------------------------------------------------------------

/** Source health, as the Sources tab shows it; hidden while there's only Uploads. */
function SourcesPanel() {
  const load = useCallback(() => api.insights<InsightsSources>("sources", rangeDays("7d")), []);
  const { data, error } = useLoad(load);
  if (!error && (!data || !data.shown)) return null;
  return (
    <Panel title="Source health" description="How your sources are syncing." error={error} loading={!data}>
      {data && (
        <Table label="Source health" head={["Source", "Health", "Last sync"]}>
          {data.sources.map((s) => (
            <tr key={s.id} className="border-b last:border-0">
              <Cell first>
                <Link to={`/data/sources/${s.id}`} className="underline underline-offset-4">
                  {s.name}
                </Link>
              </Cell>
              <Cell>{sourceHealthLabel(s.health)}</Cell>
              <Cell>{s.last_sync ? formatDate(s.last_sync.started_at) : "Never"}</Cell>
            </tr>
          ))}
        </Table>
      )}
    </Panel>
  );
}
