import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useMatch } from "react-router";
import { Plus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { describeError } from "@/lib/api";
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

/** Passed to the detail routes so they can refresh the list after a change. */
export type ListContext = { reloadList: () => Promise<void> };

/** A list of items on the left, and the selected item (or a form) on the right. */
export function MasterDetail<T extends { id: number }>({
  title,
  basePath,
  items,
  error,
  empty,
  renderItem,
  reloadList,
  canCreate = true,
}: {
  title: string;
  basePath: string;
  items: T[] | undefined;
  error?: string;
  empty: string;
  renderItem: (item: T) => ReactNode;
  reloadList: () => Promise<void>;
  /** Whether to offer a "New" button (linking to `${basePath}/new`). */
  canCreate?: boolean;
}) {
  // On small screens show either the list or the detail, not both.
  const atIndex = useMatch(basePath) !== null;
  return (
    <div className="grid h-full min-h-0 grid-cols-1 md:grid-cols-[300px_1fr]">
      <section className={cn("min-h-0 flex-col border-r", atIndex ? "flex" : "hidden md:flex")}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h1 className="text-base font-semibold">{title}</h1>
          {canCreate && (
            <Button asChild size="sm">
              <NavLink to={`${basePath}/new`}>
                <Plus />
                New
              </NavLink>
            </Button>
          )}
        </div>
        {error && <p className="text-destructive px-4 py-2 text-sm">{error}</p>}
        <ul className="flex-1 space-y-0.5 overflow-y-auto p-2">
          {items?.length === 0 && <li className="text-muted-foreground p-2 text-sm">{empty}</li>}
          {items?.map((item) => (
            <li key={item.id}>
              <NavLink
                to={`${basePath}/${item.id}`}
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
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
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
  children,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2">
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

export function formatDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
