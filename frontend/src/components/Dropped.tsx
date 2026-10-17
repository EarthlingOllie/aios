import { useCallback, useState } from "react";
import { Loader2, RotateCcw } from "lucide-react";

import { ErrorBanner, formatDate, useLoad, usePoll } from "@/components/crud";
import { Notice } from "@/components/Notice";
import { Button } from "@/components/ui/button";
import { api, describeError, type DroppedObject } from "@/lib/api";
import { droppedLabel, purgeIn } from "@/lib/dropped";

/** How often the list is read again while a restore is on its way (ms). */
const POLL_MS = 2000;
/** How long a restore is waited for before the page stops expecting it (ms). */
const RESTORE_WAIT_MS = 30_000;

/** A restore asked for: the item's restore error then, and when to stop waiting for it. */
type Restoring = { error: string | null; until: number };

/**
 * The Data page's Dropped section: the tables and columns approved drops took away, with how
 * long until each is purged, and restore. A restore is done by the platform shortly after it's
 * asked for, so the list is read again until it's gone from it (or says why it couldn't be).
 */
export function DroppedSection() {
  const list = useLoad(useCallback(() => api.listDropped(), []));
  const [restoring, setRestoring] = useState<Map<number, Restoring>>(new Map());
  const [error, setError] = useState<string>();
  const items = list.data ?? [];
  // A restore is on its way while the item's still listed with no new reason it was refused,
  // until it's been waited for long enough (the platform may have given up without one).
  const asked = (item: DroppedObject) => {
    const r = restoring.get(item.id);
    return r && r.error === item.restore_error ? r : undefined;
  };
  const busy = (item: DroppedObject) => (asked(item)?.until ?? 0) > Date.now();
  const overdue = (item: DroppedObject) => !!asked(item) && !busy(item);
  usePoll(items.some(busy), list.reload, POLL_MS);

  async function restore(item: DroppedObject) {
    setError(undefined);
    const until = Date.now() + RESTORE_WAIT_MS;
    setRestoring((r) => new Map(r).set(item.id, { error: item.restore_error, until }));
    try {
      await api.restoreDropped(item.id);
    } catch (err) {
      setError(describeError(err));
      setRestoring((r) => {
        const next = new Map(r);
        next.delete(item.id);
        return next;
      });
    }
    await list.reload();
  }

  return (
    <section aria-label="Dropped" className="flex flex-col gap-3">
      <p className="text-muted-foreground text-sm">
        Tables and columns you approved dropping. Agents can't see them, but they're kept until
        they're purged, and you can restore them until then.
      </p>
      {list.error && (
        <Notice label="Dropped" error className="px-3 py-2">
          {list.error}
        </Notice>
      )}
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {!list.data && !list.error && (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      )}
      {list.data && items.length === 0 && (
        <p className="text-muted-foreground text-sm">Nothing is dropped.</p>
      )}
      {items.length > 0 && (
        <ul className="flex flex-col gap-2">
          {items.map((item) => {
            const label = droppedLabel(item);
            const waiting = busy(item);
            return (
              <li
                key={item.id}
                aria-label={label}
                className="flex flex-col gap-1 rounded-md border px-4 py-3 text-sm"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{label}</span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="ml-auto"
                    disabled={!!item.blocked || waiting}
                    onClick={() => restore(item)}
                  >
                    {waiting ? <Loader2 className="animate-spin" /> : <RotateCcw />} Restore
                  </Button>
                </div>
                <div className="text-muted-foreground flex flex-wrap gap-x-3 text-xs">
                  <span>Dropped {formatDate(item.dropped_at)}</span>
                  <span title={formatDate(item.purge_at)}>{purgeIn(item.purge_at)}</span>
                </div>
                {item.blocked && <p className="text-muted-foreground text-xs">{item.blocked}</p>}
                {overdue(item) && (
                  <p className="text-muted-foreground text-xs">
                    It hasn't been restored yet. Reload the page in a while to see if it has, or
                    try again.
                  </p>
                )}
                {item.restore_error && !waiting && (
                  <p className="text-destructive text-xs">
                    Couldn't restore it: {item.restore_error}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
