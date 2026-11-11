import { useState } from "react";

import { ErrorBanner, Section, selectClass } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type Organisation, type Retention } from "@/lib/api";

/** Retention runs from a day to a hundred years, as the API allows. */
const MIN_DAYS = 1;
const MAX_DAYS = 36_500;

type Kind = keyof Retention;

const KINDS: { kind: Kind; label: string; what: string; when: string }[] = [
  { kind: "run_days", label: "Keep runs", what: "runs", when: "Runs are kept" },
  { kind: "chat_days", label: "Keep chat history", what: "chat history", when: "Chat history is kept" },
];

/** A setting as the form holds it: kept indefinitely, or for the days typed (maybe not a number
 * yet). */
type Draft = { forever: boolean; days: string };

function draftOf(days: number | null): Draft {
  return days === null ? { forever: true, days: "30" } : { forever: false, days: String(days) };
}

function daysOf(draft: Draft): number | null | undefined {
  if (draft.forever) return null;
  const n = Number(draft.days);
  return Number.isInteger(n) && n >= MIN_DAYS && n <= MAX_DAYS ? n : undefined;
}

function describe(days: number | null, when: string, kind: Kind): string {
  if (days === null) return `${when} indefinitely.`;
  const span = `${days} ${days === 1 ? "day" : "days"}`;
  return kind === "run_days" ? `${when} for ${span} after they finish.` : `${when} for ${span}.`;
}

/**
 * The Organisation page's retention settings: how long finished runs and chats' messages are
 * kept before the nightly job prunes them. Indefinitely by default; admins choose a number of
 * days, and everyone else sees the policy.
 */
export function RetentionSection({ org, onChanged }: { org: Organisation; onChanged: () => Promise<void> }) {
  const admin = org.role === "admin";
  const [drafts, setDrafts] = useState<Record<Kind, Draft>>(() => ({
    run_days: draftOf(org.retention.run_days),
    chat_days: draftOf(org.retention.chat_days),
  }));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>();

  const next = { run_days: daysOf(drafts.run_days), chat_days: daysOf(drafts.chat_days) };
  const valid = next.run_days !== undefined && next.chat_days !== undefined;
  const changed = next.run_days !== org.retention.run_days || next.chat_days !== org.retention.chat_days;

  function edit(kind: Kind, draft: Partial<Draft>) {
    setSaved(false);
    setDrafts((d) => ({ ...d, [kind]: { ...d[kind], ...draft } }));
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setSaving(true);
    setError(undefined);
    try {
      await api.setRetention(next as Retention);
      await onChanged();
      setSaved(true);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section title="Retention" label="Retention">
      <p className="text-muted-foreground text-sm">
        How long finished runs and chat history are kept. Anything older is pruned each night; chats themselves,
        documents, data tables, usage and the audit log are never pruned.
      </p>
      {admin ? (
        <form className="flex flex-col gap-3 rounded-md border p-3" onSubmit={onSubmit}>
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
          {KINDS.map(({ kind, label, what }) => {
            const draft = drafts[kind];
            return (
              <div key={kind} className="flex flex-wrap items-center gap-2 text-sm">
                <label htmlFor={`retention-${kind}`} className="min-w-36 font-medium">
                  {label}
                </label>
                <select
                  id={`retention-${kind}`}
                  className={selectClass + " w-auto"}
                  value={draft.forever ? "forever" : "days"}
                  disabled={saving}
                  onChange={(e) => edit(kind, { forever: e.target.value === "forever" })}
                >
                  <option value="forever">Indefinitely</option>
                  <option value="days">For a number of days</option>
                </select>
                {!draft.forever && (
                  <>
                    <Input
                      type="number"
                      aria-label={`Days to keep ${what}`}
                      className="w-28"
                      min={MIN_DAYS}
                      max={MAX_DAYS}
                      step={1}
                      required
                      value={draft.days}
                      disabled={saving}
                      onChange={(e) => edit(kind, { days: e.target.value })}
                    />
                    <span className="text-muted-foreground">days</span>
                  </>
                )}
              </div>
            );
          })}
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={saving || !valid || !changed}>
              Apply
            </Button>
            {saved && (
              <span role="status" className="text-muted-foreground text-sm">
                Saved
              </span>
            )}
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-1 rounded-md border p-3 text-sm">
          {KINDS.map(({ kind, when }) => (
            <p key={kind}>{describe(org.retention[kind], when, kind)}</p>
          ))}
          <p className="text-muted-foreground">Only admins can change how long they're kept.</p>
        </div>
      )}
    </Section>
  );
}
