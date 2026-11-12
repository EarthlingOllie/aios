import { useCallback, useState } from "react";
import { Loader2 } from "lucide-react";

import { ErrorBanner, selectClass, useLoad } from "@/components/crud";
import { Input } from "@/components/ui/input";
import { api, type UsageGroup, type UsageTotal } from "@/lib/api";
import { formatCost } from "@/lib/limits";

const GROUPS: { group: UsageGroup; label: string; column: string }[] = [
  { group: "agent", label: "By agent", column: "Agent" },
  { group: "workflow", label: "By workflow", column: "Workflow" },
  { group: "person", label: "By person", column: "Person" },
  { group: "day", label: "By day", column: "Day" },
];

/** A day `days` before today, as YYYY-MM-DD (UTC, as the ledger's days are). */
function daysAgo(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

const number = (n: number) => Math.round(n).toLocaleString("en-GB");

/** What a total's row is for: its name, or what usage with none was. */
function rowName(total: UsageTotal, group: UsageGroup): string {
  if (total.name) return total.name;
  if (total.key === null) return group === "person" ? "No one (schedules)" : `No ${group}`;
  return `#${total.key}`;
}

/**
 * The Organisation page's Usage tab, admins' only: what the organisation's agents and workflows
 * used and cost (estimated, in pounds) over a range of days, by agent, workflow, person or day.
 */
export function UsageSection() {
  const [group, setGroup] = useState<UsageGroup>("agent");
  const [start, setStart] = useState(() => daysAgo(29));
  const [end, setEnd] = useState(() => daysAgo(0));
  const load = useCallback(() => api.usage(group, start || undefined, end || undefined), [group, start, end]);
  const { data, error } = useLoad(load);
  const column = GROUPS.find((g) => g.group === group)!.column;
  const total = data?.totals.reduce((sum, t) => sum + (t.cost ?? 0), 0);

  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground text-sm">
        Every model call, embedding, web search and second of sandbox time, with its estimated cost in pounds sterling.
      </p>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col gap-1">
          <span className="font-medium">Group</span>
          <select
            aria-label="Group usage"
            className={selectClass + " w-auto"}
            value={group}
            onChange={(e) => setGroup(e.target.value as UsageGroup)}
          >
            {GROUPS.map((g) => (
              <option key={g.group} value={g.group}>
                {g.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-medium">From</span>
          <Input type="date" aria-label="From" className="w-40" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-medium">To</span>
          <Input type="date" aria-label="To" className="w-40" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
      </div>
      <ErrorBanner message={error} />
      {!data && !error ? (
        <Loader2 className="size-4 animate-spin" />
      ) : data && data.totals.length === 0 ? (
        <p className="text-muted-foreground text-sm">Nothing was used in these days.</p>
      ) : (
        data && (
          <div className="overflow-x-auto rounded-md border">
            <table aria-label="Usage totals" className="w-full text-sm">
              <thead className="bg-muted/50 text-left">
                <tr>
                  <th className="px-3 py-2 font-medium">{column}</th>
                  <th className="px-3 py-2 text-right font-medium">Model calls</th>
                  <th className="px-3 py-2 text-right font-medium">Tokens in</th>
                  <th className="px-3 py-2 text-right font-medium">Tokens out</th>
                  <th className="px-3 py-2 text-right font-medium">Web searches</th>
                  <th className="px-3 py-2 text-right font-medium">Tool calls</th>
                  <th className="px-3 py-2 text-right font-medium">Sandbox seconds</th>
                  <th className="px-3 py-2 text-right font-medium">Cost</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {data.totals.map((t) => (
                  <tr key={String(t.key)}>
                    <td className="px-3 py-2">{rowName(t, group)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{number(t.model_calls)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{number(t.input_tokens)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{number(t.output_tokens)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{number(t.web_searches)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{number(t.tool_calls)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{number(t.sandbox_seconds)}</td>
                    <td
                      className="px-3 py-2 text-right tabular-nums"
                      title={t.unpriced ? `${t.unpriced} with no price` : undefined}
                    >
                      {formatCost(t.cost)}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t font-medium">
                  <td className="px-3 py-2" colSpan={6}>
                    Total
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums" data-testid="usage-total">
                    {formatCost(total ?? 0)}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )
      )}
    </div>
  );
}
