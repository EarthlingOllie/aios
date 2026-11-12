import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { Navigate, useLocation, useNavigate, useParams } from "react-router";

import { activeTab, keyTarget, tabHref, tabRedirect } from "@/lib/tabs";
import { cn } from "@/lib/utils";

export type Tab = {
  /** Names the tab in the URL, e.g. `runs` in `/agents/3/runs`. */
  id: string;
  label: ReactNode;
  /** Shown beside the label, e.g. how many runs there are; leave it out while loading. */
  count?: number;
  content: ReactNode;
};

/**
 * Tabs whose open tab is a segment of the route (`/agents/3/runs`), so a reload, a shared link or
 * a link back from a sub-page opens on it. The page's route names it `:tab?`; the first tab is
 * the default and is the page's own path (`/agents/3`), and the first tab's segment, an unknown
 * one or an old `?tab=runs` link is sent to its canonical path. Switching navigates to the tab's
 * route, keeping the query, and replaces the history entry, so Back leaves the page rather than
 * stepping through tabs.
 * Follows the ARIA tabs pattern: the arrows, Home and End move between tabs.
 * Only the open tab's panel is mounted, so state inside a panel (an open `<details>`, a scroll
 * position, a draft) resets on switching, as it does on leaving the page; keep anything that must
 * survive above the tabs or in the URL.
 */
export function Tabs({
  tabs,
  label,
  renamed,
}: {
  tabs: Tab[];
  /** Names the tab list for screen readers. */
  label?: string;
  /** Tabs' old ids, by the id each is now: an old link opens the tab under its new name. */
  renamed?: Record<string, string>;
}) {
  const requested = useParams().tab;
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  const htmlId = useId();
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const ids = tabs.map((t) => t.id);
  // The page's own path: the URL without the tab's segment.
  const page = requested === undefined ? pathname.replace(/\/$/, "") : pathname.replace(/\/[^/]*\/?$/, "");
  const current = activeTab(ids, requested ?? null);
  const selected = tabs.find((t) => t.id === current);
  const redirect = tabRedirect(page, ids, requested, search, renamed);

  const select = (id: string) => navigate(tabHref(page, ids, id, search), { replace: true });

  const onKeyDown = (event: KeyboardEvent, index: number) => {
    const target = keyTarget(event.key, index, tabs.length);
    if (target === null) return;
    event.preventDefault();
    select(ids[target]);
    buttons.current[target]?.focus();
  };

  if (redirect !== null) return <Navigate to={redirect} replace />;
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
              id={`${htmlId}-tab-${t.id}`}
              aria-selected={isSelected}
              // Only the open tab's panel is rendered, so only it can be pointed at.
              aria-controls={isSelected ? `${htmlId}-panel-${t.id}` : undefined}
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
          id={`${htmlId}-panel-${selected.id}`}
          aria-labelledby={`${htmlId}-tab-${selected.id}`}
          tabIndex={0}
          className="focus-visible:ring-ring/50 flex flex-col gap-6 rounded-md outline-none focus-visible:ring-[3px]"
        >
          {selected.content}
        </div>
      )}
    </div>
  );
}
