import { useCallback, useEffect, useId, useState } from "react";
import { Link } from "react-router";
import { Bot, CalendarClock, Pencil, Plus, Trash2, Workflow, Zap } from "lucide-react";

import { ErrorBanner, Field, formatDate, RoleNote, Section, selectClass, useLoad } from "@/components/crud";
import { ProvenanceLine } from "@/components/provenance";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  describeError,
  type SchedulePreview,
  type Trigger,
  type TriggerFilter,
  type TriggerInput,
  type TriggerStatus,
  type TriggerTargetKind,
} from "@/lib/api";
import { useRefusal } from "@/lib/current-user";
import { browserKnowsTimeZone, formatInTimeZone } from "@/lib/timezones";
import { cn } from "@/lib/utils";

const PRESETS = [
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every day at 09:00", cron: "0 9 * * *" },
  { label: "Weekdays at 09:00", cron: "0 9 * * 1-5" },
  { label: "Every Monday at 09:00", cron: "0 9 * * 1" },
  { label: "First of the month at 09:00", cron: "0 9 1 * *" },
];


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
    timezone: t.timezone ?? "UTC",
    ...(t.kind === "event"
      ? { event_type: t.event_type, filter: t.filter, fire_on_own_events: t.fire_on_own_events }
      : {}),
    ...changes,
  };
}

/** The event types a trigger can be made for so far, as the form offers them. */
export const EVENT_TYPES = [{ value: "document_ingested", label: "A document is ingested" }] as const;

/** A comma-separated list as typed, as a filter's values: trimmed, without the empty ones. */
export function listOf(typed: string): string[] {
  return typed
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

/** What an event trigger's filter matches, in plain English ("" for everything). */
export function describeFilter(filter: TriggerFilter, sourceNames: Map<number, string> = new Map()) {
  const parts: string[] = [];
  if (filter.content_types?.length) parts.push(`of type ${filter.content_types.join(", ")}`);
  if (filter.sources?.length)
    parts.push(`from ${filter.sources.map((id) => sourceNames.get(id) ?? `source ${id}`).join(", ")}`);
  return parts.join(" ");
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
  const set = (patch: Partial<TriggerInput>) => setForm((f) => ({ ...f, ...patch }));
  const cronExpr = form.cron ?? "";
  const preset = PRESETS.find((p) => p.cron === cronExpr)?.cron ?? "custom";
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
      {!target && <TargetPicker id={`${id}-target`} form={form} set={set} />}
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
            value={cronExpr}
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
      <Preview cron={cronExpr} timezone={form.timezone} />
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

/** The agent or workflow a new trigger runs, picked from the organisation's. */
function TargetPicker({
  id,
  form,
  set,
}: {
  id: string;
  form: TriggerInput;
  set: (patch: Partial<TriggerInput>) => void;
}) {
  const agents = useLoad(api.listAgents);
  const workflows = useLoad(api.listWorkflows);
  return (
        <Field label="Runs" htmlFor={id}>
          <select
            id={id}
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
  );
}

/**
 * Creates an event trigger, or edits `trigger`: it runs its agent or workflow when a matching
 * event happens in the store. With `target` it's for that agent or workflow; without it, the
 * form lets the user pick one.
 */
export function EventTriggerEditor({
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
          kind: "event",
          target_kind: target?.kind ?? "agent",
          target_id: target?.id ?? 0,
          enabled: true,
          input: "",
          cron: null,
          timezone: "UTC",
          event_type: "document_ingested",
          filter: {},
          fire_on_own_events: false,
        },
  );
  const [contentTypes, setContentTypes] = useState((form.filter?.content_types ?? []).join(", "));
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const sources = useLoad(api.listSources);
  const set = (patch: Partial<TriggerInput>) => setForm((f) => ({ ...f, ...patch }));
  const chosenSources = form.filter?.sources ?? [];
  const isAgent = form.target_kind === "agent";

  function toggleSource(sourceId: number, on: boolean) {
    const next = on ? [...chosenSources, sourceId] : chosenSources.filter((s) => s !== sourceId);
    set({ filter: { ...form.filter, sources: next } });
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    const body: TriggerInput = {
      ...form,
      filter: { ...form.filter, content_types: listOf(contentTypes) },
    };
    try {
      onSaved(trigger ? await api.updateTrigger(trigger.id, body) : await api.createTrigger(body));
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
      aria-label={trigger ? "Edit event trigger" : "New event trigger"}
    >
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {!target && <TargetPicker id={`${id}-target`} form={form} set={set} />}
      <Field label="When" htmlFor={`${id}-event`}>
        <select
          id={`${id}-event`}
          className={selectClass}
          value={form.event_type ?? "document_ingested"}
          onChange={(e) => set({ event_type: e.target.value as TriggerInput["event_type"] })}
        >
          {EVENT_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </Field>
      <Field
        label="Content types"
        htmlFor={`${id}-types`}
        hint="Comma-separated, e.g. application/pdf, text/*. Leave empty for any."
      >
        <Input
          id={`${id}-types`}
          value={contentTypes}
          onChange={(e) => setContentTypes(e.target.value)}
          placeholder="Any"
        />
      </Field>
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1 text-sm font-medium">Sources</legend>
        <p className="text-muted-foreground text-xs">Leave all unticked for any source.</p>
        {sources.data?.map((source) => (
          <label key={source.id} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={chosenSources.includes(source.id)}
              onChange={(e) => toggleSource(source.id, e.target.checked)}
            />
            {source.name}
          </label>
        ))}
      </fieldset>
      <Field
        label={isAgent ? "Instructions" : "Input template"}
        htmlFor={`${id}-input`}
        hint={
          isAgent
            ? "Optional. Each time it fires, a new run of the agent's current version starts with this and the event."
            : "Optional. The workflow run's input, with {{payload.name}} and the like filled from the event; empty gives the event as JSON."
        }
      >
        <Textarea
          id={`${id}-input`}
          rows={3}
          value={form.input}
          onChange={(e) => set({ input: e.target.value })}
          placeholder={isAgent ? "e.g. Summarise the new document." : "e.g. File {{payload.name}}"}
        />
      </Field>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={!!form.fire_on_own_events}
          onChange={(e) => set({ fire_on_own_events: e.target.checked })}
        />
        Also fire on events its own {isAgent ? "agent" : "workflow"} caused
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        Enabled
      </label>
      <div className="flex gap-2">
        <Button type="submit" disabled={saving || !form.target_id}>
          {trigger ? "Save trigger" : "Create trigger"}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

const STATUS_LABELS: Record<TriggerStatus, string> = {
  running: "running",
  succeeded: "succeeded",
  failed: "failed",
  refused: "refused",
  rate_limited: "Rate limited",
};

function StatusBadge({ trigger }: { trigger: Trigger }) {
  if (!trigger.last_status) return null;
  const variant = {
    running: "secondary",
    succeeded: "outline",
    failed: "destructive",
    refused: "destructive",
    rate_limited: "destructive",
  } as const;
  return (
    <Badge variant={variant[trigger.last_status]} title={trigger.last_error ?? undefined}>
      {STATUS_LABELS[trigger.last_status]}
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
  sourceNames,
  busy,
  onEdit,
  onToggle,
  onDelete,
  refusal,
}: {
  trigger: Trigger;
  showTarget?: boolean;
  /** Sources' names by id, for an event trigger's filter. */
  sourceNames?: Map<number, string>;
  busy?: boolean;
  /** Why the signed-in member can't change it (`useRefusal`): the switch is disabled, saying so,
   * and editing and deleting are hidden. */
  refusal?: string | null;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const t = trigger;
  const TargetIcon = t.target_kind === "agent" ? Bot : Workflow;
  // The target's Schedule tab, where this schedule is.
  const targetHref = `/${t.target_kind === "agent" ? "agents" : "workflows"}/${t.target_id}/schedule`;
  const lastRunHref = runHref(t);
  const isEvent = t.kind === "event";
  const KindIcon = isEvent ? Zap : CalendarClock;
  const what = isEvent ? "trigger" : "schedule";
  const filter = isEvent ? describeFilter(t.filter, sourceNames) : "";
  const shownError = t.last_status && t.last_status !== "running" && t.last_status !== "succeeded";
  return (
    <li
      className={cn("flex items-start gap-3 px-3 py-2.5", !t.enabled && "opacity-60")}
      data-testid={isEvent ? "event-trigger-row" : "schedule-row"}
    >
      <KindIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1 space-y-0.5">
        {showTarget && (
          <Link to={targetHref} className="flex items-center gap-1.5 text-sm font-medium hover:underline">
            <TargetIcon className="size-3.5" />
            {t.target_name}
          </Link>
        )}
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span>{t.description}</span>
          {isEvent ? (
            filter && <span className="text-muted-foreground text-xs">{filter}</span>
          ) : (
            <>
              <code className="bg-muted rounded px-1 text-xs">{t.cron}</code>
              <span className="text-muted-foreground text-xs">{t.timezone}</span>
            </>
          )}
        </div>
        {t.input && <p className="text-muted-foreground truncate text-xs">“{t.input}”</p>}
        <p className="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          {t.next_run_at ? (
            <span data-testid="next-run">
              Next run {fromNow(t.next_run_at)} ({formatDate(t.next_run_at)})
            </span>
          ) : isEvent && t.enabled ? (
            <span>Waiting for events</span>
          ) : (
            <span>Paused{t.disabled_reason ? `: ${t.disabled_reason}` : ""}</span>
          )}
          {t.last_fired_at && (
            <span className="flex items-center gap-1" data-testid="last-outcome">
              {isEvent ? "Last fired" : "Last run"} {formatDate(t.last_fired_at)} <StatusBadge trigger={t} />
              {lastRunHref && (
                <Link to={lastRunHref} className="underline">
                  view run
                </Link>
              )}
            </span>
          )}
        </p>
        {shownError && t.last_error && <p className="text-destructive text-xs">{t.last_error}</p>}
        <ProvenanceLine of={t} />
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <label className="text-muted-foreground mr-1 flex items-center gap-1 text-xs" title={refusal ?? undefined}>
          <input
            type="checkbox"
            checked={t.enabled}
            disabled={busy || !!refusal}
            onChange={onToggle}
            aria-label={t.enabled ? `Pause ${what}` : `Resume ${what}`}
          />
          On
        </label>
        {!refusal && (
          <>
            <Button variant="ghost" size="icon" onClick={onEdit} aria-label={`Edit ${what}`}>
              <Pencil />
            </Button>
            <Button variant="ghost" size="icon" onClick={onDelete} disabled={busy} aria-label={`Delete ${what}`}>
              <Trash2 />
            </Button>
          </>
        )}
      </div>
    </li>
  );
}

/**
 * Schedules and event triggers, with adding, editing, pausing and deleting. For one agent or
 * workflow (`target`), or, without one, all of the organisation's, the next to run first.
 */
export function ScheduleList({ target, empty }: { target?: Target; empty: string }) {
  // `target` is memoised by the caller (see `TargetSchedules`), so this reloads only when it changes.
  const triggers = useLoad(useCallback(() => api.listTriggers(target), [target]));
  // Names for event triggers' source filters; without them, sources show by id.
  const sources = useLoad(api.listSources);
  const sourceNames = new Map((sources.data ?? []).map((s) => [s.id, s.name]));
  const [editing, setEditing] = useState<number | "new" | "new-event">();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const refusal = useRefusal("build");

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
      <RoleNote refusal={refusal} />
      {editing === "new" ? (
        <ScheduleEditor target={target} onSaved={saved} onCancel={() => setEditing(undefined)} />
      ) : editing === "new-event" ? (
        <EventTriggerEditor target={target} onSaved={saved} onCancel={() => setEditing(undefined)} />
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setEditing("new")}
            disabled={!!refusal}
            title={refusal ?? undefined}
          >
            <Plus />
            Add schedule
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setEditing("new-event")}
            disabled={!!refusal}
            title={refusal ?? undefined}
          >
            <Zap />
            Add event trigger
          </Button>
        </div>
      )}
      {triggers.data?.length === 0 && editing !== "new" && editing !== "new-event" && (
        <p className="text-muted-foreground text-sm">{empty}</p>
      )}
      {!!triggers.data?.length && (
        <ul className="divide-y rounded-md border">
          {triggers.data.map((t) =>
            editing === t.id ? (
              <li key={t.id} className="p-2">
                {t.kind === "event" ? (
                  <EventTriggerEditor
                    trigger={t}
                    target={target}
                    onSaved={saved}
                    onCancel={() => setEditing(undefined)}
                  />
                ) : (
                  <ScheduleEditor
                    trigger={t}
                    target={target}
                    onSaved={saved}
                    onCancel={() => setEditing(undefined)}
                  />
                )}
              </li>
            ) : (
              <ScheduleRow
                key={t.id}
                trigger={t}
                showTarget={!target}
                sourceNames={sourceNames}
                busy={busy}
                refusal={refusal}
                onEdit={() => setEditing(t.id)}
                onToggle={() =>
                  act(() => api.updateTrigger(t.id, triggerInput(t, { enabled: !t.enabled })))
                }
                onDelete={() => {
                  const what = t.kind === "event" ? "trigger" : "schedule";
                  if (window.confirm(`Delete this ${what}?`)) act(() => api.deleteTrigger(t.id));
                }}
              />
            ),
          )}
        </ul>
      )}
    </div>
  );
}

/** The "Schedules" section of an agent's or a workflow's page, with its event triggers. */
export function TargetSchedules({ kind, id }: Target) {
  const target = useTargetMemo(kind, id);
  return (
    <Section title="Schedules and triggers">
      <ScheduleList
        target={target}
        empty={`No schedules or triggers. Add one to run this ${kind} automatically.`}
      />
    </Section>
  );
}

function useTargetMemo(kind: TriggerTargetKind, id: number): Target {
  const [target, setTarget] = useState<Target>({ kind, id });
  if (target.kind !== kind || target.id !== id) setTarget({ kind, id });
  return target;
}
