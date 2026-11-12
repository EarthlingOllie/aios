import { useCallback, useEffect, useId, useState } from "react";
import { Link } from "react-router";
import { Bot, CalendarClock, KeyRound, Pencil, Plus, Trash2, Workflow, Zap } from "lucide-react";

import { ErrorBanner, Field, formatDate, RoleNote, Section, selectClass, useLoad } from "@/components/crud";
import { ProvenanceLine } from "@/components/provenance";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  describeError,
  type AuthorKind,
  type EventType,
  type SchedulePreview,
  type Trigger,
  type TriggerFilter,
  type TriggerInput,
  type TriggerStatus,
  type TriggerTargetKind,
  type WriteOperation,
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
      ? {
          event_type: t.event_type,
          filter: t.filter,
          fire_on_own_events: t.fire_on_own_events,
          debounce_seconds: t.debounce_seconds,
        }
      : {}),
    deliver_to: t.deliver_to?.id ?? null,
    ...changes,
  };
}

/**
 * Where the schedule's runs' results go: a chat (any that isn't archived; choosing one adds the
 * agent or workflow to it), or nowhere. One delivering to a chat archived since keeps it, shown
 * as archived, with a prompt to choose another.
 */
function DeliverToPicker({
  id,
  trigger,
  value,
  onChange,
}: {
  id: string;
  trigger?: Trigger;
  value: number | null;
  onChange: (chatId: number | null) => void;
}) {
  const chats = useLoad(useCallback(() => api.listChats(), []));
  const current = trigger?.deliver_to;
  const archived = current?.archived && current.id === value ? current : null;
  return (
    <Field
      label="Deliver to"
      htmlFor={id}
      hint="When a run ends, its result is summarised and posted in this chat, or a short message if it failed. Choosing a chat adds this schedule's agent or workflow to it."
    >
      <select
        id={id}
        className={selectClass}
        value={value ?? ""}
        onChange={(e) =>
          onChange(e.target.value ? Number(e.target.value) : null)
        }
      >
        <option value="">Nowhere</option>
        {archived && (
          <option value={archived.id} disabled>
            {archived.name} (archived)
          </option>
        )}
        {chats.data?.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      {archived && (
        <p role="alert" className="text-destructive mt-1 text-xs">
          {archived.name} is archived, so results aren't delivered there. Choose
          another chat.
        </p>
      )}
    </Field>
  );
}

/** The event types a trigger can be made for, as the form offers them. */
export const EVENT_TYPES: { value: EventType; label: string }[] = [
  { value: "document_ingested", label: "A document is ingested" },
  { value: "table_written", label: "A data table is written" },
  { value: "schema_change_applied", label: "A schema change is applied" },
  { value: "chat_message", label: "A message is posted in a chat" },
  { value: "webhook_received", label: "Its webhook is called" },
];

const OPERATIONS: WriteOperation[] = ["insert", "upsert", "update", "delete"];
const AUTHOR_KINDS: { value: AuthorKind; label: string }[] = [
  { value: "user", label: "People" },
  { value: "agent", label: "Agents" },
  { value: "workflow", label: "Workflows" },
];

/** A comma-separated list as typed, as a filter's values: trimmed, without the empty ones. */
export function listOf(typed: string): string[] {
  return typed
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

/** What an event trigger's filter matches, in plain English ("" for everything). */
export function describeFilter(
  filter: TriggerFilter,
  sourceNames: Map<number, string> = new Map(),
  chatNames: Map<number, string> = new Map(),
) {
  const parts: string[] = [];
  if (filter.chat_id) parts.push(`in ${chatNames.get(filter.chat_id) ?? `chat ${filter.chat_id}`}`);
  if (filter.tables?.length) parts.push(`to ${filter.tables.join(", ")}`);
  if (filter.operations?.length) parts.push(`by ${filter.operations.join(", ")}`);
  if (filter.content_types?.length) parts.push(`of type ${filter.content_types.join(", ")}`);
  if (filter.sources?.length)
    parts.push(`from ${filter.sources.map((id) => sourceNames.get(id) ?? `source ${id}`).join(", ")}`);
  if (filter.author_kinds?.length) parts.push(`written by ${filter.author_kinds.join(", ")}`);
  if (filter.mentions?.length) parts.push(`mentioning ${filter.mentions.join(", ")}`);
  if (filter.keywords?.length) parts.push(`saying ${filter.keywords.map((k) => `"${k}"`).join(" or ")}`);
  return parts.join(" ");
}

/** The filter an event trigger's form sends for its event type: only that type's fields, the
 * typed lists split on commas. */
export function filterFor(
  eventType: EventType,
  filter: TriggerFilter,
  typed: { contentTypes: string; tables: string; mentions: string; keywords: string },
): TriggerFilter {
  switch (eventType) {
    case "document_ingested":
      return { sources: filter.sources ?? [], content_types: listOf(typed.contentTypes) };
    case "table_written":
      return { tables: listOf(typed.tables), operations: filter.operations ?? [] };
    case "schema_change_applied":
      return { tables: listOf(typed.tables) };
    case "chat_message":
      return {
        ...(filter.chat_id ? { chat_id: filter.chat_id } : {}),
        author_kinds: filter.author_kinds ?? [],
        mentions: listOf(typed.mentions),
        keywords: listOf(typed.keywords),
      };
    case "webhook_received":
      return {};
  }
}

/** A list of `values` to toggle `value` in or out of. */
function toggled<T>(values: T[] | undefined, value: T, on: boolean): T[] {
  const rest = (values ?? []).filter((v) => v !== value);
  return on ? [...rest, value] : rest;
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
      <DeliverToPicker
        id={`${id}-deliver-to`}
        trigger={trigger}
        value={form.deliver_to ?? null}
        onChange={(deliver_to) => set({ deliver_to })}
      />
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
  const [tables, setTables] = useState((form.filter?.tables ?? []).join(", "));
  const [mentions, setMentions] = useState((form.filter?.mentions ?? []).join(", "));
  const [keywords, setKeywords] = useState((form.filter?.keywords ?? []).join(", "));
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const eventType: EventType = form.event_type ?? "document_ingested";
  const sources = useLoad(api.listSources);
  const chats = useLoad(useCallback(() => api.listChats(), []));
  const set = (patch: Partial<TriggerInput>) => setForm((f) => ({ ...f, ...patch }));
  const setFilter = (patch: Partial<TriggerFilter>) => set({ filter: { ...form.filter, ...patch } });
  const filter = form.filter ?? {};
  const chosenSources = filter.sources ?? [];
  const isAgent = form.target_kind === "agent";

  function toggleSource(sourceId: number, on: boolean) {
    setFilter({ sources: toggled(chosenSources, sourceId, on) });
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    const body: TriggerInput = {
      ...form,
      filter: filterFor(eventType, filter, { contentTypes, tables, mentions, keywords }),
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
          value={eventType}
          onChange={(e) => set({ event_type: e.target.value as EventType, filter: {} })}
        >
          {EVENT_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </Field>
      {eventType === "document_ingested" && (
        <>
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
        </>
      )}
      {(eventType === "table_written" || eventType === "schema_change_applied") && (
        <Field
          label="Tables"
          htmlFor={`${id}-tables`}
          hint="Comma-separated data table names, e.g. orders, invoices. Leave empty for any."
        >
          <Input id={`${id}-tables`} value={tables} onChange={(e) => setTables(e.target.value)} placeholder="Any" />
        </Field>
      )}
      {eventType === "table_written" && (
        <fieldset className="flex flex-col gap-1">
          <legend className="mb-1 text-sm font-medium">Operations</legend>
          <p className="text-muted-foreground text-xs">Leave all unticked for any operation.</p>
          {OPERATIONS.map((op) => (
            <label key={op} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={(filter.operations ?? []).includes(op)}
                onChange={(e) => setFilter({ operations: toggled(filter.operations, op, e.target.checked) })}
              />
              {op}
            </label>
          ))}
        </fieldset>
      )}
      {eventType === "chat_message" && (
        <>
          <Field
            label="Chat"
            htmlFor={`${id}-chat`}
            hint="Any of the organisation's chats, whether or not you're in it. Saving adds this trigger's agent or workflow to it."
          >
            <select
              id={`${id}-chat`}
              required
              className={selectClass}
              value={filter.chat_id ?? ""}
              onChange={(e) => setFilter({ chat_id: e.target.value ? Number(e.target.value) : undefined })}
            >
              <option value="" disabled>
                Choose a chat…
              </option>
              {chats.data
                ?.filter((c) => !c.archived_at || c.id === filter.chat_id)
                .map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
            </select>
          </Field>
          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-sm font-medium">Written by</legend>
            <p className="text-muted-foreground text-xs">Leave all unticked for anyone.</p>
            {AUTHOR_KINDS.map((k) => (
              <label key={k.value} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={(filter.author_kinds ?? []).includes(k.value)}
                  onChange={(e) =>
                    setFilter({ author_kinds: toggled(filter.author_kinds, k.value, e.target.checked) })
                  }
                />
                {k.label}
              </label>
            ))}
          </fieldset>
          <Field
            label="Keywords"
            htmlFor={`${id}-keywords`}
            hint="Comma-separated. Matches whole words, ignoring case: outage doesn't match outages. Leave empty for any message."
          >
            <Input
              id={`${id}-keywords`}
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              placeholder="Any"
            />
          </Field>
          <Field
            label="Mentions"
            htmlFor={`${id}-mentions`}
            hint="Comma-separated, e.g. agent:3, user:7. Leave empty for any."
          >
            <Input
              id={`${id}-mentions`}
              value={mentions}
              onChange={(e) => setMentions(e.target.value)}
              placeholder="Any"
            />
          </Field>
        </>
      )}
      {eventType === "webhook_received" && (
        <p className="text-muted-foreground text-sm">
          It gets its own URL and signing secret, shown once it's created. Callers sign each call;
          Jev checks every body before anything fires.
        </p>
      )}
      <Field
        label="Gather events for (seconds)"
        htmlFor={`${id}-debounce`}
        hint="Optional. Matches within this window start one run, with all of them (up to 500). Leave empty to run on each."
      >
        <Input
          id={`${id}-debounce`}
          type="number"
          min={1}
          max={86400}
          value={form.debounce_seconds ?? ""}
          onChange={(e) => set({ debounce_seconds: e.target.value ? Number(e.target.value) : null })}
          placeholder="Run on each"
        />
      </Field>
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
        <Button
          type="submit"
          disabled={saving || !form.target_id || (eventType === "chat_message" && !filter.chat_id)}
        >
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

/**
 * A webhook trigger's signing secret, shown once: as it's made, and as it's rotated. Callers
 * sign each call with it; it can't be shown again.
 */
export function WebhookSecret({ trigger, onDone }: { trigger: Trigger; onDone: () => void }) {
  return (
    <section
      role="region"
      aria-label="Webhook secret"
      className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/5 p-3 text-sm"
    >
      <p className="font-medium">Copy the signing secret now: it won't be shown again.</p>
      <Field label="URL" htmlFor={`webhook-url-${trigger.id}`}>
        <Input id={`webhook-url-${trigger.id}`} readOnly className="font-mono text-xs" value={trigger.webhook_url ?? ""} />
      </Field>
      <Field label="Signing secret" htmlFor={`webhook-secret-${trigger.id}`}>
        <Input
          id={`webhook-secret-${trigger.id}`}
          readOnly
          className="font-mono text-xs"
          value={trigger.webhook_secret ?? ""}
        />
      </Field>
      <p className="text-muted-foreground text-xs">
        POST JSON to the URL with <code>X-Aios-Timestamp</code> (Unix seconds) and{" "}
        <code>X-Aios-Signature: sha256=&lt;hex&gt;</code>, the HMAC-SHA256 of{" "}
        <code>&lt;timestamp&gt;.&lt;body&gt;</code> keyed by the secret. An optional{" "}
        <code>X-Aios-Delivery</code> id makes retries safe.
      </p>
      <div>
        <Button type="button" size="sm" onClick={onDone}>
          I've copied it
        </Button>
      </div>
    </section>
  );
}

export function ScheduleRow({
  trigger,
  showTarget,
  sourceNames,
  chatNames,
  busy,
  onEdit,
  onToggle,
  onDelete,
  onRotate,
  refusal,
}: {
  trigger: Trigger;
  showTarget?: boolean;
  /** Sources' names by id, for an event trigger's filter. */
  sourceNames?: Map<number, string>;
  /** Chats' names by id, for a chat message trigger's filter. */
  chatNames?: Map<number, string>;
  busy?: boolean;
  /** Why the signed-in member can't change it (`useRefusal`): the switch is disabled, saying so,
   * and editing and deleting are hidden. */
  refusal?: string | null;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
  /** Rotate a webhook trigger's secret. */
  onRotate?: () => void;
}) {
  const t = trigger;
  const TargetIcon = t.target_kind === "agent" ? Bot : Workflow;
  // The target's Schedule tab, where this schedule is.
  const targetHref = `/${t.target_kind === "agent" ? "agents" : "workflows"}/${t.target_id}/schedule`;
  const lastRunHref = runHref(t);
  const isEvent = t.kind === "event";
  const KindIcon = isEvent ? Zap : CalendarClock;
  const what = isEvent ? "trigger" : "schedule";
  const filter = isEvent ? describeFilter(t.filter, sourceNames, chatNames) : "";
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
        {t.debounce_seconds && (
          <p className="text-muted-foreground text-xs" data-testid="debounce">
            Gathers events for {t.debounce_seconds}s into one run
          </p>
        )}
        {t.webhook_url && (
          <p className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs" data-testid="webhook-url">
            <code className="bg-muted rounded px-1 break-all">{t.webhook_url}</code>
            {!refusal && onRotate && (
              <Button variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={onRotate} disabled={busy}>
                <KeyRound className="size-3" />
                Rotate secret
              </Button>
            )}
          </p>
        )}
        {t.deliver_to && (
          <p className="text-muted-foreground text-xs" data-testid="deliver-to">
            Delivers to{" "}
            <Link to={`/chats/${t.deliver_to.id}`} className="underline">
              {t.deliver_to.name}
            </Link>
            {t.deliver_to.archived && " (archived)"}
            {t.deliver_to_set_by && ` · chosen by ${t.deliver_to_set_by.name}`}
          </p>
        )}
        {t.delivery_problem && (
          <p role="alert" className="text-destructive text-xs" data-testid="delivery-problem">
            {t.delivery_problem}
          </p>
        )}
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
  // Names for chat message triggers' chats.
  const chats = useLoad(useCallback(() => api.listChats(), []));
  const chatNames = new Map((chats.data ?? []).map((c) => [c.id, c.name]));
  const [editing, setEditing] = useState<number | "new" | "new-event">();
  // A webhook trigger just made or rotated, whose secret is shown until it's dismissed.
  const [revealed, setRevealed] = useState<Trigger>();
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

  const saved = (t: Trigger) => {
    setEditing(undefined);
    if (t.webhook_secret) setRevealed(t);
    triggers.reload();
  };

  return (
    <div className="flex flex-col gap-3">
      <ErrorBanner message={error ?? triggers.error} onDismiss={() => setError(undefined)} />
      <RoleNote refusal={refusal} />
      {revealed && <WebhookSecret trigger={revealed} onDone={() => setRevealed(undefined)} />}
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
                chatNames={chatNames}
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
                onRotate={() => {
                  if (
                    window.confirm(
                      "Rotate the secret? The URL and secret in use stop working at once.",
                    )
                  )
                    act(async () => setRevealed(await api.rotateWebhookSecret(t.id)));
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
