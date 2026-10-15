import { useCallback, useEffect, useId, useState } from "react";
import { Link } from "react-router";
import { Bot, CalendarClock, Pencil, Plus, Trash2, Workflow } from "lucide-react";

import { ErrorBanner, Field, formatDate, Section, useLoad } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  describeError,
  type SchedulePreview,
  type Trigger,
  type TriggerInput,
  type TriggerTargetKind,
} from "@/lib/api";
import { browserKnowsTimeZone, formatInTimeZone } from "@/lib/timezones";
import { cn } from "@/lib/utils";

const PRESETS = [
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every day at 09:00", cron: "0 9 * * *" },
  { label: "Weekdays at 09:00", cron: "0 9 * * 1-5" },
  { label: "Every Monday at 09:00", cron: "0 9 * * 1" },
  { label: "First of the month at 09:00", cron: "0 9 1 * *" },
];

const selectClass = "border-input dark:bg-input/30 h-9 w-full rounded-md border bg-transparent px-2 text-sm";

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** E.g. "in 3 hours", "in 2 days". */
export function fromNow(iso: string, now = Date.now()) {
  const seconds = (new Date(iso).getTime() - now) / 1000;
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return rtf.format(Math.round(seconds), "second");
}

export function triggerInput(t: Trigger, changes: Partial<TriggerInput> = {}): TriggerInput {
  return {
    kind: t.kind,
    target_kind: t.target_kind,
    target_id: t.target_id,
    enabled: t.enabled,
    input: t.input,
    cron: t.cron,
    timezone: t.timezone,
    ...changes,
  };
}

/** What the cron expression means, and when it next runs; checked by the server as it's typed. */
function Preview({ cron, timezone }: { cron: string; timezone: string }) {
  // The preview keeps the timezone it was fetched for: until the next check, `timezone` may be
  // half typed, and the runs are shown in the zone they were computed in, not that one.
  const [state, setState] = useState<{
    preview?: SchedulePreview;
    timezone?: string;
    error?: string;
  }>({});
  useEffect(() => {
    if (!cron.trim() || !timezone.trim()) {
      setState({});
      return;
    }
    let stale = false;
    const timer = setTimeout(() => {
      api.previewSchedule(cron, timezone).then(
        (preview) => !stale && setState({ preview, timezone }),
        (err) => !stale && setState({ error: describeError(err) }),
      );
    }, 250);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [cron, timezone]);

  if (state.error)
    return (
      <p className="text-destructive text-sm" data-testid="schedule-preview-error">
        {state.error}
      </p>
    );
  if (!state.preview || !state.timezone) return null;
  const zone = state.timezone;
  return (
    <div className="bg-muted/50 rounded-md border p-3 text-sm" data-testid="schedule-preview">
      <p className="font-medium">{state.preview.description}</p>
      <p className="text-muted-foreground mt-1 text-xs">
        {browserKnowsTimeZone(zone)
          ? `Next runs (${zone}):`
          : `Next runs (in UTC: this browser can't show times in ${zone}):`}
      </p>
      <ul className="text-muted-foreground mt-1 space-y-0.5 text-xs">
        {state.preview.next_runs.map((t) => (
          <li key={t}>{formatInTimeZone(t, zone)}</li>
        ))}
      </ul>
    </div>
  );
}

type Target = { kind: TriggerTargetKind; id: number };

/**
 * Creates a schedule, or edits `trigger`. With `target` the schedule is for that agent or
 * workflow; without it, the form lets the user pick one.
 */
export function ScheduleEditor({
  trigger,
  target,
  onSaved,
  onCancel,
}: {
  trigger?: Trigger;
  target?: Target;
  onSaved: (t: Trigger) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const [form, setForm] = useState<TriggerInput>(() =>
    trigger
      ? triggerInput(trigger)
      : {
          kind: "schedule",
          target_kind: target?.kind ?? "agent",
          target_id: target?.id ?? 0,
          enabled: true,
          input: "",
          cron: "0 9 * * *",
          timezone: browserTimezone(),
        },
  );
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const timezones = useLoad(api.listTimezones);
  const agents = useLoad(useCallback(() => (target ? Promise.resolve([]) : api.listAgents()), [target]));
  const workflows = useLoad(
    useCallback(() => (target ? Promise.resolve([]) : api.listWorkflows()), [target]),
  );
  const set = (patch: Partial<TriggerInput>) => setForm((f) => ({ ...f, ...patch }));
  const preset = PRESETS.find((p) => p.cron === form.cron)?.cron ?? "custom";
  const isAgent = form.target_kind === "agent";

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      onSaved(trigger ? await api.updateTrigger(trigger.id, form) : await api.createTrigger(form));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      className="flex flex-col gap-4 rounded-md border p-4"
      onSubmit={onSubmit}
      aria-label={trigger ? "Edit schedule" : "New schedule"}
    >
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {!target && (
        <Field label="Runs" htmlFor={`${id}-target`}>
          <select
            id={`${id}-target`}
            required
            className={selectClass}
            value={form.target_id ? `${form.target_kind}:${form.target_id}` : ""}
            onChange={(e) => {
              const [kind, targetId] = e.target.value.split(":");
              set({ target_kind: kind as TriggerTargetKind, target_id: Number(targetId) });
            }}
          >
            <option value="" disabled>
              Choose an agent or a workflow…
            </option>
            <optgroup label="Agents">
              {agents.data?.map((a) => (
                <option key={`a${a.id}`} value={`agent:${a.id}`}>
                  {a.name}
                </option>
              ))}
            </optgroup>
            <optgroup label="Workflows">
              {workflows.data?.map((w) => (
                <option key={`w${w.id}`} value={`workflow:${w.id}`}>
                  {w.name}
                </option>
              ))}
            </optgroup>
          </select>
        </Field>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Repeats" htmlFor={`${id}-preset`}>
          <select
            id={`${id}-preset`}
            className={selectClass}
            value={preset}
            onChange={(e) => e.target.value !== "custom" && set({ cron: e.target.value })}
          >
            {PRESETS.map((p) => (
              <option key={p.cron} value={p.cron}>
                {p.label}
              </option>
            ))}
            <option value="custom">Custom (cron)</option>
          </select>
        </Field>
        <Field
          label="Cron expression"
          htmlFor={`${id}-cron`}
          hint="minute hour day-of-month month day-of-week, or @hourly, @daily…"
        >
          <Input
            id={`${id}-cron`}
            required
            className="font-mono"
            value={form.cron}
            onChange={(e) => set({ cron: e.target.value })}
          />
        </Field>
      </div>
      <Field label="Timezone" htmlFor={`${id}-tz`}>
        <Input
          id={`${id}-tz`}
          required
          list={`${id}-tz-list`}
          value={form.timezone}
          onChange={(e) => set({ timezone: e.target.value })}
        />
        <datalist id={`${id}-tz-list`}>
          {timezones.data?.map((tz) => <option key={tz} value={tz} />)}
        </datalist>
      </Field>
      <Preview cron={form.cron} timezone={form.timezone} />
      <Field
        label={isAgent ? "Message" : "Input"}
        htmlFor={`${id}-input`}
        hint={
          isAgent
            ? "Each time the schedule fires, a new run of the agent's current version starts with this message."
            : "Each time the schedule fires, a run of the workflow's current version starts with this input."
        }
      >
        <Textarea
          id={`${id}-input`}
          required
          rows={3}
          value={form.input}
          onChange={(e) => set({ input: e.target.value })}
          placeholder={
            isAgent ? "e.g. Summarise yesterday's support tickets." : "e.g. Yesterday's support tickets."
          }
        />
      </Field>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        Enabled
      </label>
      <div className="flex gap-2">
        <Button type="submit" disabled={saving || !form.target_id || !form.input.trim()}>
          {trigger ? "Save schedule" : "Create schedule"}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function StatusBadge({ trigger }: { trigger: Trigger }) {
  if (!trigger.last_status) return null;
  const variant = { running: "secondary", succeeded: "outline", failed: "destructive" } as const;
  return (
    <Badge variant={variant[trigger.last_status]} title={trigger.last_error ?? undefined}>
      {trigger.last_status}
    </Badge>
  );
}

/** The page of the run the trigger last started, if it has one. */
function runHref(t: Trigger): string | undefined {
  if (t.target_kind === "agent" && t.last_run_id) return `/agents/${t.target_id}/runs/${t.last_run_id}`;
  if (t.target_kind === "workflow" && t.last_workflow_run_id)
    return `/workflows/${t.target_id}/runs/${t.last_workflow_run_id}`;
  return undefined;
}

export function ScheduleRow({
  trigger,
  showTarget,
  busy,
  onEdit,
  onToggle,
  onDelete,
}: {
  trigger: Trigger;
  showTarget?: boolean;
  busy?: boolean;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const t = trigger;
  const TargetIcon = t.target_kind === "agent" ? Bot : Workflow;
  const targetHref = `/${t.target_kind === "agent" ? "agents" : "workflows"}/${t.target_id}`;
  const lastRunHref = runHref(t);
  return (
    <li className={cn("flex items-start gap-3 px-3 py-2.5", !t.enabled && "opacity-60")} data-testid="schedule-row">
      <CalendarClock className="text-muted-foreground mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1 space-y-0.5">
        {showTarget && (
          <Link to={targetHref} className="flex items-center gap-1.5 text-sm font-medium hover:underline">
            <TargetIcon className="size-3.5" />
            {t.target_name}
          </Link>
        )}
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span>{t.description}</span>
          <code className="bg-muted rounded px-1 text-xs">{t.cron}</code>
          <span className="text-muted-foreground text-xs">{t.timezone}</span>
        </div>
        {t.input && <p className="text-muted-foreground truncate text-xs">“{t.input}”</p>}
        <p className="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          {t.next_run_at ? (
            <span data-testid="next-run">
              Next run {fromNow(t.next_run_at)} ({formatDate(t.next_run_at)})
            </span>
          ) : (
            <span>Paused</span>
          )}
          {t.last_fired_at && (
            <span className="flex items-center gap-1">
              Last run {formatDate(t.last_fired_at)} <StatusBadge trigger={t} />
              {lastRunHref && (
                <Link to={lastRunHref} className="underline">
                  view run
                </Link>
              )}
            </span>
          )}
        </p>
        {t.last_status === "failed" && t.last_error && (
          <p className="text-destructive text-xs">{t.last_error}</p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <label className="text-muted-foreground mr-1 flex items-center gap-1 text-xs">
          <input
            type="checkbox"
            checked={t.enabled}
            disabled={busy}
            onChange={onToggle}
            aria-label={t.enabled ? "Pause schedule" : "Resume schedule"}
          />
          On
        </label>
        <Button variant="ghost" size="icon" onClick={onEdit} aria-label="Edit schedule">
          <Pencil />
        </Button>
        <Button variant="ghost" size="icon" onClick={onDelete} disabled={busy} aria-label="Delete schedule">
          <Trash2 />
        </Button>
      </div>
    </li>
  );
}

/**
 * Schedules, with adding, editing, pausing and deleting. For one agent or workflow (`target`),
 * or, without one, all of the user's, the next to run first.
 */
export function ScheduleList({ target, empty }: { target?: Target; empty: string }) {
  // `target` is memoised by the caller (see `TargetSchedules`), so this reloads only when it changes.
  const triggers = useLoad(useCallback(() => api.listTriggers(target), [target]));
  const [editing, setEditing] = useState<number | "new">();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await fn();
      await triggers.reload();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  const saved = () => {
    setEditing(undefined);
    triggers.reload();
  };

  return (
    <div className="flex flex-col gap-3">
      <ErrorBanner message={error ?? triggers.error} onDismiss={() => setError(undefined)} />
      {editing === "new" ? (
        <ScheduleEditor target={target} onSaved={saved} onCancel={() => setEditing(undefined)} />
      ) : (
        <div>
          <Button size="sm" variant="outline" onClick={() => setEditing("new")}>
            <Plus />
            Add schedule
          </Button>
        </div>
      )}
      {triggers.data?.length === 0 && editing !== "new" && (
        <p className="text-muted-foreground text-sm">{empty}</p>
      )}
      {!!triggers.data?.length && (
        <ul className="divide-y rounded-md border">
          {triggers.data.map((t) =>
            editing === t.id ? (
              <li key={t.id} className="p-2">
                <ScheduleEditor
                  trigger={t}
                  target={target}
                  onSaved={saved}
                  onCancel={() => setEditing(undefined)}
                />
              </li>
            ) : (
              <ScheduleRow
                key={t.id}
                trigger={t}
                showTarget={!target}
                busy={busy}
                onEdit={() => setEditing(t.id)}
                onToggle={() =>
                  act(() => api.updateTrigger(t.id, triggerInput(t, { enabled: !t.enabled })))
                }
                onDelete={() => {
                  if (window.confirm("Delete this schedule?")) act(() => api.deleteTrigger(t.id));
                }}
              />
            ),
          )}
        </ul>
      )}
    </div>
  );
}

/** The "Schedules" section of an agent's or a workflow's page. */
export function TargetSchedules({ kind, id }: Target) {
  const target = useTargetMemo(kind, id);
  return (
    <Section title="Schedules">
      <ScheduleList
        target={target}
        empty={`No schedules. Add one to run this ${kind} automatically.`}
      />
    </Section>
  );
}

function useTargetMemo(kind: TriggerTargetKind, id: number): Target {
  const [target, setTarget] = useState<Target>({ kind, id });
  if (target.kind !== kind || target.id !== id) setTarget({ kind, id });
  return target;
}
