import { useState } from "react";

import { ErrorBanner, Section, selectClass } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type InvocableBy, type LimitName, type Limits, type Organisation, type Role } from "@/lib/api";
import {
  aboveCeiling,
  ALL_LIMITS,
  draftOf,
  formatLimit,
  INVOCABLE_BY,
  invocableByLabel,
  LIMIT_LABELS,
  limitsOf,
  type LimitsDraft,
} from "@/lib/limits";

/**
 * The organisation's caps on turns and workflow runs, on the Settings tab: each is the
 * deployment's default unless an admin overrides it here. Agents and workflows follow them
 * unless they set lower ones of their own; only admins set them higher. Everyone else reads them.
 */
export function OrganisationLimitsSection({ org, onChanged }: { org: Organisation; onChanged: () => Promise<void> }) {
  const admin = org.role === "admin";
  const [draft, setDraft] = useState<LimitsDraft<LimitName>>(() => draftOf(ALL_LIMITS, org.limits.overrides));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>();
  const next = limitsOf(ALL_LIMITS, draft);
  const changed = next !== undefined && ALL_LIMITS.some((n) => next[n] !== org.limits.overrides[n]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!next) return;
    setSaving(true);
    setError(undefined);
    try {
      await api.setLimits(next as Limits);
      await onChanged();
      setSaved(true);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section title="Limits" label="Limits">
      <p className="text-muted-foreground text-sm">
        A turn or a workflow run stops before a model call that would pass one of these, saying which limit it reached
        and keeping what it got to. Agents and workflows may set lower limits of their own.
      </p>
      {admin ? (
        <form className="flex flex-col gap-3 rounded-md border p-3" onSubmit={onSubmit}>
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
          {ALL_LIMITS.map((name) => (
            <div key={name} className="flex flex-wrap items-center gap-2 text-sm">
              <label htmlFor={`limit-${name}`} className="min-w-56 font-medium">
                {LIMIT_LABELS[name]}
              </label>
              <Input
                id={`limit-${name}`}
                type="number"
                className="w-36"
                min={0}
                step={name.endsWith("_cost") ? "0.01" : 1}
                placeholder={String(org.limits.defaults[name])}
                value={draft[name]}
                disabled={saving}
                onChange={(e) => {
                  setSaved(false);
                  setDraft((d) => ({ ...d, [name]: e.target.value }));
                }}
              />
              <span className="text-muted-foreground text-xs">
                Default {formatLimit(name, org.limits.defaults[name]!)}
              </span>
            </div>
          ))}
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={saving || !next || !changed}>
              Apply
            </Button>
            {!next && <span className="text-destructive text-sm">Each limit must be a number above zero.</span>}
            {saved && (
              <span role="status" className="text-muted-foreground text-sm">
                Saved
              </span>
            )}
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-1 rounded-md border p-3 text-sm">
          <dl aria-label="Limits in force" className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
            {ALL_LIMITS.map((name) => (
              <div key={name} className="contents">
                <dt>{LIMIT_LABELS[name]}</dt>
                <dd className="tabular-nums">{formatLimit(name, org.limits.in_force[name]!)}</dd>
              </div>
            ))}
          </dl>
          <p className="text-muted-foreground">Only admins can change the organisation's limits.</p>
        </div>
      )}
    </Section>
  );
}

/**
 * An agent's or workflow's form fields for who may invoke it by mentioning it and its own caps
 * (`names`): blank follows the organisation's (`ceiling`). A member who isn't an admin is told
 * when a cap is above the organisation's, which only admins may set.
 */
export function GuardFields<N extends LimitName>({
  kind,
  names,
  invocableBy,
  onInvocableBy,
  draft,
  onDraft,
  ceiling,
  saved,
  role,
}: {
  kind: "agent" | "workflow";
  names: readonly N[];
  invocableBy: InvocableBy;
  onInvocableBy: (value: InvocableBy) => void;
  draft: LimitsDraft<N>;
  onDraft: (draft: LimitsDraft<N>) => void;
  /** The organisation's caps in force; undefined until loaded. */
  ceiling: Limits | undefined;
  /** The caps as saved, which may be kept as they are. */
  saved: Partial<Limits>;
  role: Role | undefined;
}) {
  const values = limitsOf(names, draft);
  const above = values && ceiling && role !== "admin" ? aboveCeiling(values, ceiling, saved) : [];
  return (
    <fieldset className="flex flex-col gap-3" aria-label="Invocation and limits">
      <legend className="mb-1.5 text-sm font-medium">Who can use it, and its limits</legend>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label htmlFor={`${kind}-invocable-by`} className="min-w-56">
          Who can mention it
        </label>
        <select
          id={`${kind}-invocable-by`}
          className={selectClass + " w-auto"}
          value={invocableBy}
          onChange={(e) => onInvocableBy(e.target.value as InvocableBy)}
        >
          {INVOCABLE_BY.map((v) => (
            <option key={v} value={v}>
              {invocableByLabel(v)}
            </option>
          ))}
        </select>
      </div>
      {names.map((name) => (
        <div key={name} className="flex flex-wrap items-center gap-2 text-sm">
          <label htmlFor={`${kind}-limit-${name}`} className="min-w-56">
            {LIMIT_LABELS[name]}
          </label>
          <Input
            id={`${kind}-limit-${name}`}
            type="number"
            className="w-36"
            min={0}
            step={name.endsWith("_cost") ? "0.01" : 1}
            placeholder={ceiling ? String(ceiling[name]) : undefined}
            value={draft[name]}
            onChange={(e) => onDraft({ ...draft, [name]: e.target.value })}
          />
          {ceiling && (
            <span className="text-muted-foreground text-xs">
              Organisation's: {formatLimit(name, ceiling[name]!)}
            </span>
          )}
        </div>
      ))}
      <p className="text-muted-foreground text-xs">Leave a limit blank to follow the organisation's.</p>
      {values === undefined && (
        <p role="alert" className="text-destructive text-sm">
          Each limit must be a number above zero, and counts whole numbers.
        </p>
      )}
      {above.length > 0 && (
        <p role="alert" className="text-destructive text-sm">
          Only admins can set a limit above the organisation's ({above.map((n) => LIMIT_LABELS[n]).join(", ")}).
        </p>
      )}
    </fieldset>
  );
}

/** Whether `GuardFields`' draft can be saved by this role. */
export function guardsValid<N extends LimitName>(
  names: readonly N[],
  draft: LimitsDraft<N>,
  ceiling: Limits | undefined,
  saved: Partial<Limits>,
  role: Role | undefined,
): boolean {
  const values = limitsOf(names, draft);
  if (!values) return false;
  return role === "admin" || !ceiling || aboveCeiling(values, ceiling, saved).length === 0;
}
