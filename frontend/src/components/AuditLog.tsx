import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { ErrorBanner, formatDate, selectClass } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type AuditEntry, type AuditFilters, type Organisation } from "@/lib/api";
import { actorName, AUDIT_ACTIONS, detailsText, targetText } from "@/lib/audit";

/**
 * The Organisation page's Audit log tab, admins' only: who did what, to what and when, newest
 * first, filtered by who did it (a member), the action (or its family: "agent") and the days
 * between, a page at a time.
 */
export function AuditLogSection({ org }: { org: Organisation }) {
  const [filters, setFilters] = useState<AuditFilters>({});
  const [entries, setEntries] = useState<AuditEntry[]>();
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (before?: string) => {
      setLoading(true);
      setError(undefined);
      try {
        const page = await api.auditLog(filters, before);
        setEntries((shown) => (before ? [...(shown ?? []), ...page.entries] : page.entries));
        setNext(page.next);
      } catch (err) {
        setError(describeError(err));
      } finally {
        setLoading(false);
      }
    },
    [filters],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const set = (change: Partial<AuditFilters>) => setFilters((f) => ({ ...f, ...change }));

  return (
    <div className="flex flex-col gap-4">
      <form
        aria-label="Filter the audit log"
        className="grid grid-cols-1 gap-2 sm:grid-cols-4"
        onSubmit={(e) => e.preventDefault()}
      >
        <label className="flex flex-col gap-1 text-xs">
          Actor
          <select
            aria-label="Actor"
            className={selectClass}
            value={filters.actor ?? ""}
            onChange={(e) => set({ actor: e.target.value ? Number(e.target.value) : undefined })}
          >
            <option value="">Anyone</option>
            {org.members.map((m) => (
              <option key={m.user.id} value={m.user.id}>
                {m.user.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Action
          <Input
            aria-label="Action"
            list="audit-actions"
            placeholder="e.g. agent, member.role_changed"
            value={filters.action ?? ""}
            onChange={(e) => set({ action: e.target.value || undefined })}
          />
          <datalist id="audit-actions">
            {AUDIT_ACTIONS.map((a) => (
              <option key={a} value={a} />
            ))}
          </datalist>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          From
          <Input
            type="date"
            value={filters.since ?? ""}
            onChange={(e) => set({ since: e.target.value || undefined })}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          To
          <Input
            type="date"
            value={filters.until ?? ""}
            onChange={(e) => set({ until: e.target.value || undefined })}
          />
        </label>
      </form>
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {entries === undefined && !error && (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      )}
      {entries !== undefined && entries.length === 0 && (
        <p className="text-muted-foreground text-sm">Nothing matches. Try other filters.</p>
      )}
      {entries !== undefined && entries.length > 0 && (
        <ul aria-label="Audit log" className="divide-y rounded-md border">
          {entries.map((e) => {
            const details = detailsText(e);
            return (
              <li key={e.id} className="flex flex-col gap-0.5 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <code className="font-mono text-xs">{e.action}</code>
                  <span className="font-medium">{actorName(e)}</span>
                  <span className="text-muted-foreground">{targetText(e)}</span>
                  <time className="text-muted-foreground ml-auto text-xs" dateTime={e.at}>
                    {formatDate(e.at)}
                  </time>
                </div>
                {details && <div className="text-muted-foreground text-xs break-words">{details}</div>}
              </li>
            );
          })}
        </ul>
      )}
      {next && (
        <Button variant="outline" size="sm" className="self-start" disabled={loading} onClick={() => load(next)}>
          Show older
        </Button>
      )}
    </div>
  );
}
