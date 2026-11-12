import { useState } from "react";

import { ErrorBanner, Section } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type Budget, type MemberSpend, type Role } from "@/lib/api";
import { amountOf, budgetState, formatPounds, monthLabel } from "@/lib/budget";
import { roleLabel } from "@/lib/permissions";

const ROLES: Role[] = ["admin", "editor", "participant"];

const LEVEL_NOTES = {
  none: null,
  ok: null,
  warning: "Spending is past the warning threshold.",
  reached: "The budget has been reached: new work is refused until next month, while work already going finishes.",
} as const;

/**
 * The organisation's monthly budget, on the Settings tab: this month's spend against it, and each
 * role's default allowance. Admins set them, taking effect at once; everyone else reads them.
 */
export function BudgetSection({
  budget,
  admin,
  onChanged,
}: {
  budget: Budget;
  admin: boolean;
  onChanged: (budget: Budget) => void;
}) {
  const [draft, setDraft] = useState(() => draftOf(budget));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>();
  const amount = amountOf(draft.budget);
  const allowances = Object.fromEntries(ROLES.map((r) => [r, amountOf(draft[r])])) as Record<
    Role,
    number | null | undefined
  >;
  const valid = amount !== undefined && ROLES.every((r) => allowances[r] !== undefined);
  const changed =
    amount !== budget.budget || ROLES.some((r) => allowances[r] !== (budget.role_allowances[r] ?? null));
  const state = budgetState(budget.spent, budget.budget, budget.warning_threshold);
  const note = LEVEL_NOTES[state.level];

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setSaving(true);
    setError(undefined);
    try {
      const saved = await api.setBudget(amount as number | null, allowances as Record<Role, number | null>);
      onChanged(saved);
      setDraft(draftOf(saved));
      setSaved(true);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section title="Budget" label="Budget">
      <p className="text-muted-foreground text-sm">
        Spending is estimated in pounds from the price table, per calendar month (UTC). At the budget, or a person's
        allowance, new work is refused before any model is called; work already going finishes. Admins are warned at{" "}
        {Math.round(budget.warning_threshold * 100)}%.
      </p>
      <dl aria-label="This month's spend" className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-md border p-3 text-sm">
        <dt>{monthLabel(budget.month)}</dt>
        <dd className="tabular-nums">
          {budget.budget === null
            ? `${formatPounds(budget.spent)} spent, no budget set`
            : `${formatPounds(budget.spent)} of ${formatPounds(budget.budget)} (${Math.round((state.share ?? 0) * 100)}%)`}
        </dd>
        {!admin &&
          ROLES.map((r) => (
            <div key={r} className="contents">
              <dt>{roleLabel(r)} allowance</dt>
              <dd className="tabular-nums">
                {budget.role_allowances[r] == null ? "None" : formatPounds(budget.role_allowances[r]!)}
              </dd>
            </div>
          ))}
      </dl>
      {note && (
        <p role="note" className={state.level === "reached" ? "text-destructive text-sm" : "text-sm"}>
          {note}
        </p>
      )}
      {admin && (
        <form className="flex flex-col gap-3 rounded-md border p-3" onSubmit={onSubmit}>
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
          <AmountField
            id="budget-amount"
            label="Monthly budget (£)"
            value={draft.budget}
            disabled={saving}
            onChange={(value) => {
              setSaved(false);
              setDraft((d) => ({ ...d, budget: value }));
            }}
          />
          {ROLES.map((r) => (
            <AmountField
              key={r}
              id={`allowance-${r}`}
              label={`${roleLabel(r)} allowance (£)`}
              value={draft[r]}
              disabled={saving}
              onChange={(value) => {
                setSaved(false);
                setDraft((d) => ({ ...d, [r]: value }));
              }}
            />
          ))}
          <p className="text-muted-foreground text-xs">
            Leave one blank for none. A person's allowance is charged with what they start, and what that starts in
            turn; schedules and events with no person behind them count only against the budget.
          </p>
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={saving || !valid || !changed}>
              Update budget
            </Button>
            {!valid && <span className="text-destructive text-sm">Each must be a sum of pounds, or blank.</span>}
            {saved && (
              <span role="status" className="text-muted-foreground text-sm">
                Saved
              </span>
            )}
          </div>
        </form>
      )}
    </Section>
  );
}

type Draft = Record<"budget" | Role, string>;

function draftOf(budget: Budget): Draft {
  const text = (v: number | null | undefined) => (v == null ? "" : String(v));
  return {
    budget: text(budget.budget),
    admin: text(budget.role_allowances.admin),
    editor: text(budget.role_allowances.editor),
    participant: text(budget.role_allowances.participant),
  };
}

function AmountField({
  id,
  label,
  value,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <label htmlFor={id} className="min-w-56 font-medium">
        {label}
      </label>
      <Input
        id={id}
        type="number"
        className="w-36"
        min={0}
        step="0.01"
        placeholder="None"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

/** What a member's line on the Members tab says of their allowance and spend this month. */
export function spendText(spend: MemberSpend): string {
  const spent = `${formatPounds(spend.spent)} this month`;
  if (spend.allowance_in_force === null) return `${spent}, no allowance`;
  const own = spend.allowance !== null ? "own " : "";
  return `${spent} of ${formatPounds(spend.allowance_in_force)} ${own}allowance`;
}

/**
 * An admin's control on the Members tab for a member's own allowance: blank follows their role's.
 */
export function AllowanceEditor({
  spend,
  name,
  onChanged,
}: {
  spend: MemberSpend;
  name: string;
  onChanged: (budget: Budget) => void;
}) {
  const [value, setValue] = useState(spend.allowance === null ? "" : String(spend.allowance));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const amount = amountOf(value);
  const changed = amount !== undefined && amount !== spend.allowance;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (amount === undefined) return;
    setSaving(true);
    setError(undefined);
    try {
      onChanged(await api.setAllowance(spend.user.id, amount));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="flex items-center gap-1" onSubmit={onSubmit} aria-label={`${name}'s allowance`}>
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <Input
        aria-label={`${name}'s own allowance (£)`}
        type="number"
        className="h-8 w-24"
        min={0}
        step="0.01"
        placeholder="Role's"
        value={value}
        disabled={saving}
        onChange={(e) => setValue(e.target.value)}
      />
      <Button type="submit" size="sm" variant="outline" disabled={saving || !changed}>
        Set
      </Button>
    </form>
  );
}
