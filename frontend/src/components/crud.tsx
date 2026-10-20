import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  NavLink,
  Outlet,
  useLocation,
  useMatch,
  useNavigationType,
  useSearchParams,
} from "react-router";
import { Plus, Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { describeError } from "@/lib/api";
import {
  chosenOption,
  listHref,
  narrowList,
  type ListFilterGroup,
  type ListSort,
} from "@/lib/list-filter";
import { cn } from "@/lib/utils";

/**
 * Loads `fn()` on mount and whenever `fn` changes; `reload` re-runs it.
 *
 * When `fn` changes, `data` and `error` are cleared straight away, so the
 * previous item is never shown (or saved) under the new one. Only the latest
 * request may update state: a slower, older response is dropped.
 */
export function useLoad<T>(fn: () => Promise<T>) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loadedFn, setLoadedFn] = useState(() => fn);
  if (loadedFn !== fn) {
    // Reset during render, so not even one frame shows the old data.
    setLoadedFn(() => fn);
    setData(undefined);
    setError(undefined);
  }
  const latest = useRef(0);
  const reload = useCallback(() => {
    const request = ++latest.current;
    return fn().then(
      (value) => {
        if (request !== latest.current) return;
        setData(value);
        setError(undefined);
      },
      (err) => {
        if (request !== latest.current) return;
        setError(describeError(err));
      },
    );
  }, [fn]);
  useEffect(() => {
    reload();
    // Unmounting or switching loaders makes any request in flight stale.
    return () => {
      latest.current++;
    };
  }, [reload]);
  return { data, error, reload };
}

/** How often something that's still going (a workflow run, say) is re-read. */
export const POLL_MS = 1000;

/**
 * While `active`, calls `reload` again `ms` (by default `POLL_MS`) after each call settles,
 * failed or not, so one failed read doesn't stop the polling.
 */
export function usePoll(active: boolean, reload: () => Promise<unknown>, ms = POLL_MS) {
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      timer = setTimeout(() => reload().finally(() => stopped || poll()), ms);
    };
    poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [active, reload, ms]);
}

/** Whether the page is visible: false while its tab is in the background or minimised. */
export function useVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

/** A native `<select>` styled like `Input`. */
export const selectClass =
  "border-input dark:bg-input/30 h-9 w-full rounded-md border bg-transparent px-2 text-sm";

/** Passed to the detail routes so they can refresh the list after a change. */
export type ListContext = { reloadList: () => Promise<void> };

/**
 * `path` with the list's search (`?q=`) from the current URL, for a section's own navigation
 * back to its list, e.g. after a delete, so the list stays filtered.
 */
export function useListHref(): (path: string) => string {
  const [params] = useSearchParams();
  return (path) => listHref(path, params);
}

/** Marks the history entries the search box writes as you type (see `useListSearch`). */
const TYPED = "listSearchTyped";

/**
 * The list's search, kept in the URL's `q` (left out when empty). Typing replaces the history
 * entry, so Back goes to the previous search rather than the previous key press. The box keeps
 * its own text, since the router updates the URL in a transition, a moment later; it takes the
 * URL's again on any other navigation (Back, a link), but not on the entries its typing wrote.
 */
function useListSearch() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigationType = useNavigationType();
  const query = params.get("q") ?? "";
  const [text, setText] = useState(query);
  const [seenKey, setSeenKey] = useState(location.key);
  const state = location.state as Record<string, unknown> | null;
  if (location.key !== seenKey) {
    setSeenKey(location.key);
    if (navigationType === "POP" || state?.[TYPED] !== true) setText(query);
  }
  const change = (value: string) => {
    setText(value);
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set("q", value);
        else next.delete("q");
        return next;
      },
      // Keeping whatever state the entry already had.
      { replace: true, state: { ...state, [TYPED]: true } },
    );
  };
  /**
   * Sets the list's other parameters (a filter, the sort; undefined removes one) as a new
   * history entry, so Back goes to the previous choice. The search goes with it as typed, even
   * if the URL hasn't caught up with the typing yet, or is cleared with `clearSearch`.
   */
  const choose = (changes: Record<string, string | undefined>, clearSearch = false) => {
    const value = clearSearch ? "" : text;
    setText(value);
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [key, choice] of Object.entries({ ...changes, q: value || undefined })) {
        if (choice === undefined) next.delete(key);
        else next.set(key, choice);
      }
      return next;
    });
  };
  return { text, change, choose };
}

/** One filter group: its buttons (one pressed at a time, the default first) or a select. */
function FilterGroup<T>({
  group,
  value,
  onChange,
}: {
  group: ListFilterGroup<T>;
  value: string | undefined;
  onChange: (key: string | undefined) => void;
}) {
  if (group.kind === "select") {
    return (
      <select
        aria-label={group.label}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || undefined)}
        className={selectClass}
      >
        <option value="">{group.all}</option>
        {group.options.map((option) => (
          <option key={option.key} value={option.key}>
            {option.label}
          </option>
        ))}
      </select>
    );
  }
  const buttons = [{ key: undefined, label: group.all }, ...group.options];
  return (
    <div role="group" aria-label={group.label} className="flex flex-wrap gap-1">
      {buttons.map((option) => (
        <Button
          key={option.key ?? ""}
          type="button"
          size="sm"
          variant={value === option.key ? "secondary" : "ghost"}
          aria-pressed={value === option.key}
          onClick={() => onChange(option.key)}
          className="h-7 px-2 text-xs"
        >
          {option.label}
        </Button>
      ))}
    </div>
  );
}

/** Whether a key press is already going into something you type in, where `/` should type. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/**
 * Pressing `/` anywhere outside a text field focuses `input` and selects its text, unless it's
 * hidden (on a small screen showing only the detail).
 */
function useSlashToFocus(input: RefObject<HTMLInputElement | null>, enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      const box = input.current;
      if (!box || box.getClientRects().length === 0 || isTyping(e.target)) return;
      e.preventDefault();
      box.focus();
      box.select();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [input, enabled]);
}

/**
 * A list of items on the left, and the selected item (or a form) on the right.
 *
 * Given `search` (the fields of an item to match, such as its name and description), the list
 * gets a search box, kept in the URL (`?q=`). Each of `filters` is a group of buttons or a
 * select, kept in its own parameter (each must be one of `LIST_PARAMS`), and `sorts` a sort
 * menu (`?sort=`), the first being the default. They combine, and are carried by the list's
 * links and New button. The open item stays open even when they hide it from the list. Pressing
 * `/` jumps to the search box.
 */
export function MasterDetail<T extends { id: number }>({
  title,
  basePath,
  items,
  error,
  empty,
  renderItem,
  reloadList,
  search,
  filters = [],
  sorts = [],
}: {
  title: string;
  basePath: string;
  items: T[] | undefined;
  error?: string;
  empty: string;
  renderItem: (item: T) => ReactNode;
  reloadList: () => Promise<void>;
  search?: (item: T) => string[];
  filters?: ListFilterGroup<T>[];
  sorts?: ListSort<T>[];
}) {
  // On small screens show either the list or the detail, not both.
  const atIndex = useMatch(basePath) !== null;
  const href = useListHref();
  const { text, change, choose } = useListSearch();
  const [params] = useSearchParams();
  const input = useRef<HTMLInputElement>(null);
  useSlashToFocus(input, search !== undefined);
  const query = search ? text.trim() : "";
  const shown = items && narrowList(items, params, { query, search, filters, sorts });
  const chosen = (options: { key: string }[], param: string) =>
    chosenOption(options, params.get(param))?.key;
  const filtered = filters.some((group) => chosen(group.options, group.param) !== undefined);
  const sortKey = chosen(sorts, "sort") ?? sorts[0]?.key;
  const noun = title.toLowerCase();
  const clear = () => {
    change("");
    input.current?.focus();
  };
  // Clear filters resets the search, the filters and the sort, as a step Back can undo.
  const clearAll = () => {
    const params = [...filters.map((group) => group.param), "sort"];
    choose(Object.fromEntries(params.map((param) => [param, undefined])), true);
    input.current?.focus();
  };
  return (
    <div className="grid h-full min-h-0 grid-cols-1 md:grid-cols-[300px_1fr]">
      <section className={cn("min-h-0 flex-col border-r", atIndex ? "flex" : "hidden md:flex")}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h1 className="text-base font-semibold">{title}</h1>
          <Button asChild size="sm">
            <NavLink to={href(`${basePath}/new`)}>
              <Plus />
              New
            </NavLink>
          </Button>
        </div>
        {(search || filters.length > 0 || sorts.length > 0) && (
          <div className="flex flex-col gap-1.5 border-b p-2">
            {search && (
              <div className="relative">
                <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
                <Input
                  ref={input}
                  type="search"
                  aria-label={`Search ${noun}`}
                  placeholder="Search"
                  value={text}
                  onChange={(e) => change(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape" && text) {
                      e.preventDefault();
                      change("");
                    }
                  }}
                  className="px-8 [&::-webkit-search-cancel-button]:hidden"
                />
                {!text && (
                  <kbd
                    aria-hidden
                    title="Press / to search"
                    className="text-muted-foreground pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 rounded border px-1.5 font-mono text-xs"
                  >
                    /
                  </kbd>
                )}
                {text && (
                  <button
                    type="button"
                    onClick={clear}
                    aria-label="Clear search"
                    className="text-muted-foreground hover:text-foreground absolute top-1/2 right-2.5 -translate-y-1/2"
                  >
                    <X className="size-4" />
                  </button>
                )}
              </div>
            )}
            {filters.map((group) => (
              <FilterGroup
                key={group.param}
                group={group}
                value={chosen(group.options, group.param)}
                onChange={(key) => choose({ [group.param]: key })}
              />
            ))}
            {(sorts.length > 0 || (items && shown!.length < items.length)) && (
              <div className="flex min-h-7 items-center gap-2">
                <p className="text-muted-foreground flex-1 px-1 text-xs">
                  {items && shown!.length < items.length && `${shown!.length} of ${items.length}`}
                </p>
                {sorts.length > 0 && (
                  <select
                    aria-label={`Sort ${noun}`}
                    value={sortKey}
                    onChange={(e) => {
                      const key = e.target.value;
                      // The default is left out of the URL.
                      choose({ sort: key === sorts[0].key ? undefined : key });
                    }}
                    className={cn(selectClass, "h-7 w-auto text-xs")}
                  >
                    {sorts.map((option) => (
                      <option key={option.key} value={option.key}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            )}
          </div>
        )}
        {error && <p className="text-destructive px-4 py-2 text-sm">{error}</p>}
        <ul aria-label={title} className="flex-1 space-y-0.5 overflow-y-auto p-2">
          {items?.length === 0 && <li className="text-muted-foreground p-2 text-sm">{empty}</li>}
          {items?.length !== 0 && shown?.length === 0 && (
            <li className="text-muted-foreground flex flex-col items-start gap-2 p-2 text-sm">
              {query
                ? `No ${noun} match “${query}”${filtered ? " with these filters" : ""}.`
                : `No ${noun} match these filters.`}
              <Button type="button" variant="outline" size="sm" onClick={clearAll}>
                Clear filters
              </Button>
            </li>
          )}
          {shown?.map((item) => (
            <li key={item.id}>
              <NavLink
                to={href(`${basePath}/${item.id}`)}
                className={({ isActive }) =>
                  cn(
                    "hover:bg-accent block rounded-md px-3 py-2 text-sm",
                    isActive && "bg-accent",
                  )
                }
              >
                {renderItem(item)}
              </NavLink>
            </li>
          ))}
        </ul>
      </section>
      <section className={cn("min-h-0 overflow-y-auto", atIndex && "hidden md:block")}>
        <Outlet context={{ reloadList } satisfies ListContext} />
      </section>
    </div>
  );
}

export function Placeholder({ children }: { children: ReactNode }) {
  return (
    <div className="text-muted-foreground flex h-full items-center justify-center p-8 text-sm">
      {children}
    </div>
  );
}

export function DetailPage({
  title,
  subtitle,
  actions,
  wide,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  /** Use more of the width, e.g. for a graph. */
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className={cn("mx-auto flex flex-col gap-6 px-4 py-6 md:px-8", wide ? "max-w-6xl" : "max-w-3xl")}
    >
      <header className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-xl font-semibold break-words">{title}</h2>
          {subtitle && <div className="text-muted-foreground mt-1 text-sm">{subtitle}</div>}
        </div>
        {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
      </header>
      {children}
    </div>
  );
}

export function Section({
  title,
  actions,
  label,
  children,
}: {
  title: string;
  actions?: ReactNode;
  /** Names the section as a landmark region, so it can be found by name. */
  label?: string;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2" aria-label={label}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
          {title}
        </h3>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-sm font-medium">
        {label}
      </label>
      {children}
      {hint && <p className="text-muted-foreground text-xs">{hint}</p>}
    </div>
  );
}

export function ErrorBanner({ message, onDismiss }: { message?: string; onDismiss?: () => void }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="border-destructive/50 text-destructive flex items-start gap-2 rounded-md border px-3 py-2 text-sm"
    >
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {onDismiss && (
        <button className="hover:text-foreground shrink-0" onClick={onDismiss} aria-label="Dismiss">
          <X className="size-4" />
        </button>
      )}
    </div>
  );
}

// Built once: `toLocaleString` with options builds a new formatter on every call, which costs
// hundreds of microseconds, and lists call this once per row on every render.
const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDate(iso: string) {
  return dateFormat.format(new Date(iso));
}
