import { useState } from "react";

import { ErrorBanner, Section } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type Organisation } from "@/lib/api";

/** An approval waits from a day to a year, as the API allows. */
const MIN_DAYS = 1;
const MAX_DAYS = 365;

const daysText = (days: number) => `${days} ${days === 1 ? "day" : "days"}`;

/**
 * The Organisation page's approval expiry: how many days an approval (a schema change, or a
 * workflow's approval step) waits undecided before it expires, which counts as not approved.
 * Admins change it; everyone else sees it. An approval node may set its own.
 */
export function ApprovalExpirySection({
  org,
  onChanged,
}: {
  org: Organisation;
  onChanged: () => Promise<void>;
}) {
  const admin = org.role === "admin";
  const [days, setDays] = useState(String(org.approval_expiry_days));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>();
  const n = Number(days);
  const valid = Number.isInteger(n) && n >= MIN_DAYS && n <= MAX_DAYS;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setSaving(true);
    setError(undefined);
    try {
      await api.setApprovalExpiry(n);
      await onChanged();
      setSaved(true);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section title="Approval expiry" label="Approval expiry">
      <p className="text-muted-foreground text-sm">
        Approvals left undecided for {daysText(org.approval_expiry_days)} expire, which counts as not
        approved. A workflow's approval step may set its own.
      </p>
      {admin && (
        <form className="flex flex-wrap items-center gap-2 text-sm" onSubmit={onSubmit}>
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
          <label htmlFor="approval-expiry-days" className="font-medium">
            Expire after
          </label>
          <Input
            id="approval-expiry-days"
            type="number"
            className="w-24"
            min={MIN_DAYS}
            max={MAX_DAYS}
            step={1}
            required
            value={days}
            disabled={saving}
            onChange={(e) => {
              setSaved(false);
              setDays(e.target.value);
            }}
          />
          <span className="text-muted-foreground">days</span>
          <Button
            type="submit"
            size="sm"
            disabled={saving || !valid || n === org.approval_expiry_days}
          >
            Set expiry
          </Button>
          {saved && <span role="status">Saved.</span>}
        </form>
      )}
    </Section>
  );
}
