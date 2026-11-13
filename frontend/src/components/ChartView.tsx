import { useEffect, useRef, useState } from "react";

import { chartTheme, drawnSpec, type ChartRow, type ChartSpec, type ColorScheme } from "@/lib/charts";

const DARK = "(prefers-color-scheme: dark)";

/** The app's colour scheme (it follows the system's), kept up to date as that changes. */
function useColorScheme(): ColorScheme {
  const [scheme, setScheme] = useState<ColorScheme>(() =>
    window.matchMedia?.(DARK).matches ? "dark" : "light",
  );
  useEffect(() => {
    const query = window.matchMedia?.(DARK);
    if (!query) return;
    const changed = (e: MediaQueryListEvent) => setScheme(e.matches ? "dark" : "light");
    query.addEventListener("change", changed);
    return () => query.removeEventListener("change", changed);
  }, []);
  return scheme;
}

// Charts load nothing: their data is inline, and any load is refused.
const refuse = () => Promise.reject(new Error("Charts don't load anything."));
const NO_LOADING = { load: refuse, sanitize: refuse, http: refuse, file: refuse };

/**
 * A chart drawn by the pinned Vega-Lite runtime (loaded when first needed), with its CSP-safe
 * expression interpreter rather than compiled expressions, in the app's light or dark theme.
 */
export function ChartView({ title, spec, rows }: { title: string; spec: ChartSpec; rows: ChartRow[] }) {
  const el = useRef<HTMLDivElement>(null);
  const scheme = useColorScheme();
  const [error, setError] = useState<string>();

  useEffect(() => {
    let finalize: (() => void) | undefined;
    let cancelled = false;
    setError(undefined);
    (async () => {
      const [{ default: embed }, { expressionInterpreter }] = await Promise.all([
        import("vega-embed"),
        import("vega-interpreter"),
      ]);
      if (cancelled || !el.current) return;
      const result = await embed(el.current, drawnSpec(spec, rows) as never, {
        renderer: "svg",
        actions: false,
        ast: true,
        expr: expressionInterpreter,
        config: chartTheme(scheme) as never,
        loader: NO_LOADING as never,
      } as never);
      if (cancelled) result.finalize();
      else finalize = () => result.finalize();
    })().catch((err: unknown) => {
      if (!cancelled) setError(err instanceof Error ? err.message : String(err));
    });
    return () => {
      cancelled = true;
      finalize?.();
    };
  }, [spec, rows, scheme, title]);

  return (
    <figure aria-label={title} className="w-full">
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          The chart couldn't be drawn: {error}
        </p>
      ) : null}
      <div ref={el} className="w-full" data-testid="chart" />
    </figure>
  );
}
