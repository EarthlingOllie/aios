import { useCallback, useState, type FormEvent } from "react";
import { Loader2 } from "lucide-react";

import { ChartView } from "@/components/ChartView";
import { ErrorBanner, Section, useLoad } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type AskedChart, type SavedChart } from "@/lib/api";
import { truncationNote } from "@/lib/charts";

/**
 * Charts on the Insights page: any member asks for one in words, sees the SQL behind it, and
 * may save it for the whole organisation. Saved charts re-run their SQL on current data each
 * time they're shown; their creator or an admin renames or deletes them.
 */
export function ChartsPanel() {
  const { data: saved, error, reload } = useLoad(api.listCharts);
  return (
    <Section title="Charts" label="Charts">
      <AskChart onSaved={reload} />
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : saved === undefined ? (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      ) : saved.length === 0 ? (
        <p className="text-muted-foreground text-sm">No saved charts yet.</p>
      ) : (
        <ul className="flex flex-col gap-4" aria-label="Saved charts">
          {saved.map((chart) => (
            <SavedChartCard key={chart.id} chart={chart} onChanged={reload} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/** The SQL behind a chart, which its viewer can always open. */
function SqlDisclosure({ sql, open = false }: { sql: string; open?: boolean }) {
  return (
    <details open={open} className="text-sm">
      <summary className="text-muted-foreground cursor-pointer select-none">SQL</summary>
      <pre className="bg-muted mt-1 overflow-x-auto rounded-md p-2 text-xs whitespace-pre-wrap">
        <code>{sql}</code>
      </pre>
    </details>
  );
}

function Truncated({ truncated, rows }: { truncated: boolean; rows: number }) {
  const note = truncationNote(truncated, rows);
  return note ? <p className="text-muted-foreground text-xs">{note}</p> : null;
}

function AskChart({ onSaved }: { onSaved: () => Promise<unknown> }) {
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [asked, setAsked] = useState<AskedChart>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  async function ask(e: FormEvent) {
    e.preventDefault();
    if (!question.trim()) return;
    setAsking(true);
    setError(undefined);
    setAsked(undefined);
    setSaved(false);
    try {
      setAsked(await api.askChart(question.trim()));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setAsking(false);
    }
  }

  async function save() {
    if (!asked?.title || !asked.sql || !asked.spec) return;
    setSaving(true);
    setError(undefined);
    try {
      await api.saveChart({ title: asked.title, question: asked.question, sql: asked.sql, spec: asked.spec });
      setSaved(true);
      await onSaved();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <form className="flex gap-2" onSubmit={ask}>
        <Input
          aria-label="Ask for a chart"
          placeholder="Ask for a chart, e.g. turns per day by agent this month"
          value={question}
          maxLength={2000}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <Button type="submit" disabled={asking || !question.trim()}>
          {asking ? <Loader2 className="size-4 animate-spin" aria-label="Asking" /> : "Ask"}
        </Button>
      </form>
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {asked && (
        <div className="flex flex-col gap-2 rounded-md border p-3" aria-label="Asked chart" role="group">
          {asked.title && <h4 className="text-sm font-semibold">{asked.title}</h4>}
          {asked.error ? (
            <p role="alert" className="text-destructive text-sm">
              {asked.error}
            </p>
          ) : (
            asked.spec &&
            asked.data && (
              <>
                <ChartView title={asked.title ?? asked.question} spec={asked.spec} rows={asked.data} />
                <Truncated truncated={asked.truncated} rows={asked.data.length} />
              </>
            )
          )}
          {asked.sql && <SqlDisclosure sql={asked.sql} open={!!asked.error} />}
          {!asked.error && (
            <div>
              {saved ? (
                <p className="text-muted-foreground text-sm">Saved to the organisation's charts.</p>
              ) : (
                <Button variant="outline" size="sm" onClick={save} disabled={saving}>
                  Save for the organisation
                </Button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SavedChartCard({ chart, onChanged }: { chart: SavedChart; onChanged: () => Promise<unknown> }) {
  const load = useCallback(() => api.chartData(chart.id), [chart.id]);
  const { data, error } = useLoad(load);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(chart.title);
  const [actionError, setActionError] = useState<string>();

  async function rename(e: FormEvent) {
    e.preventDefault();
    setActionError(undefined);
    try {
      await api.renameChart(chart.id, title.trim());
      setRenaming(false);
      await onChanged();
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  async function remove() {
    if (!window.confirm(`Delete the chart "${chart.title}" for everyone?`)) return;
    setActionError(undefined);
    try {
      await api.deleteChart(chart.id);
      await onChanged();
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  return (
    <li className="flex flex-col gap-2 rounded-md border p-3" aria-label={chart.title}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        {renaming ? (
          <form className="flex gap-2" onSubmit={rename}>
            <Input aria-label="Chart title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
            <Button type="submit" size="sm" disabled={!title.trim()}>
              Save
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setRenaming(false)}>
              Cancel
            </Button>
          </form>
        ) : (
          <h4 className="text-sm font-semibold">{chart.title}</h4>
        )}
        {chart.can_change && !renaming && (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" onClick={() => setRenaming(true)}>
              Rename
            </Button>
            <Button variant="ghost" size="sm" onClick={remove}>
              Delete
            </Button>
          </div>
        )}
      </div>
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />
      {chart.question && <p className="text-muted-foreground text-xs">“{chart.question}”</p>}
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          This chart's query no longer works.
        </p>
      ) : data === undefined ? (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      ) : (
        <>
          <ChartView title={chart.title} spec={chart.spec} rows={data.data} />
          <Truncated truncated={data.truncated} rows={data.data.length} />
        </>
      )}
      <SqlDisclosure sql={chart.sql} open={!!error} />
      {error && <p className="text-muted-foreground text-xs">{error}</p>}
      {chart.created_by && (
        <p className="text-muted-foreground text-xs">Saved by {chart.created_by.name}</p>
      )}
    </li>
  );
}
