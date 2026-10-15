import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router";

import { activeTab, keyTarget } from "@/lib/tabs";
import { cn } from "@/lib/utils";

export type Tab = {
  /** Names the tab in the URL, e.g. `?tab=runs`. */
  id: string;
  label: ReactNode;
  /** Shown beside the label, e.g. how many runs there are; leave it out while loading. */
  count?: number;
  content: ReactNode;
};

/**
 * Tabs that keep the open tab in the URL's `param` (`?tab=runs`), so a reload, a shared link or
 * a link back from a sub-page opens on it. The first tab is the default and leaves the URL bare.
 * Switching replaces the history entry, so Back leaves the page rather than stepping through tabs.
 * Follows the ARIA tabs pattern: the arrows, Home and End move between tabs.
 * Only the open tab's panel is mounted, so state inside a panel (an open `<details>`, a scroll
 * position, a draft) resets on switching, as it does on leaving the page; keep anything that must
 * survive above the tabs or in the URL.
 */
export function Tabs({
  tabs,
  param = "tab",
  label,
}: {
  tabs: Tab[];
  /** The search parameter that holds the open tab; give each set of tabs on a page its own. */
  param?: string;
  /** Names the tab list for screen readers. */
  label?: string;
}) {
  const [params, setParams] = useSearchParams();
  const base = useId();
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const ids = tabs.map((t) => t.id);
  const current = activeTab(ids, params.get(param));
  const selected = tabs.find((t) => t.id === current);

  const select = (id: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (id === ids[0]) next.delete(param);
        else next.set(param, id);
        return next;
      },
      { replace: true },
    );

  const onKeyDown = (event: KeyboardEvent, index: number) => {
    const target = keyTarget(event.key, index, tabs.length);
    if (target === null) return;
    event.preventDefault();
    select(ids[target]);
    buttons.current[target]?.focus();
  };

  return (
    <div className="flex flex-col gap-6">
      <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto border-b">
        {tabs.map((t, i) => {
          const isSelected = t.id === current;
          return (
            <button
              key={t.id}
              ref={(el) => {
                buttons.current[i] = el;
              }}
              type="button"
              role="tab"
              id={`${base}-tab-${t.id}`}
              aria-selected={isSelected}
              // Only the open tab's panel is rendered, so only it can be pointed at.
              aria-controls={isSelected ? `${base}-panel-${t.id}` : undefined}
              tabIndex={isSelected ? 0 : -1}
              onClick={() => select(t.id)}
              onKeyDown={(e) => onKeyDown(e, i)}
              className={cn(
                "focus-visible:ring-ring/50 -mb-px flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium outline-none focus-visible:ring-[3px]",
                isSelected
                  ? "border-foreground text-foreground"
                  : "text-muted-foreground hover:text-foreground border-transparent",
              )}
            >
              {t.label}
              {t.count !== undefined && (
                <>
                  {" "}
                  <span className="bg-muted text-muted-foreground rounded-full px-1.5 text-xs tabular-nums">
                    {t.count}
                  </span>
                </>
              )}
            </button>
          );
        })}
      </div>
      {selected && (
        <div
          role="tabpanel"
          id={`${base}-panel-${selected.id}`}
          aria-labelledby={`${base}-tab-${selected.id}`}
          className="flex flex-col gap-6"
        >
          {selected.content}
        </div>
      )}
    </div>
  );
}
