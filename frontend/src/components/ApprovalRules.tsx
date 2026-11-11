import { useState } from "react";
import { Loader2 } from "lucide-react";

import { ErrorBanner, Section, selectClass, useLoad } from "@/components/crud";
import { api, describeError, type ApprovalRule, type Role } from "@/lib/api";
import { useRefusal } from "@/lib/current-user";
import { roleLabel } from "@/lib/permissions";

/** From the least role that may decide a change to the most: a rule offers its default and up. */
const RAISES: Role[] = ["editor", "admin"];

/**
 * The Organisation page's approval rules: who may decide each kind of schema change. Every
 * member sees them; admins raise a kind's role (never below its default, which the picker
 * doesn't offer).
 */
export function ApprovalRulesSection() {
  const { data: rules, error: loadError, reload } = useLoad(api.listApprovalRules);
  const refusal = useRefusal("run_organisation");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function change(rule: ApprovalRule, role: Role) {
    setBusy(true);
    setError(undefined);
    try {
      await api.setApprovalRule(rule.kind, role);
      await reload();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Approval rules" label="Approval rules">
      <p className="text-muted-foreground text-sm">
        Who may approve each kind of change an agent asks to make to the organisation's tables. A
        request needs the highest role among its changes.
      </p>
      <ErrorBanner message={error ?? loadError} onDismiss={() => setError(undefined)} />
      {!rules && !loadError && <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />}
      {rules && (
        <ul className="divide-y rounded-md border">
          {rules.map((rule) => (
            <li key={rule.kind} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span className="min-w-0 flex-1">{rule.label}</span>
              {refusal === null ? (
                <select
                  aria-label={rule.label}
                  className={selectClass + " w-auto"}
                  value={rule.role}
                  disabled={busy}
                  onChange={(e) => change(rule, e.target.value as Role)}
                >
                  {RAISES.slice(RAISES.indexOf(rule.default_role)).map((role) => (
                    <option key={role} value={role}>
                      {roleLabel(role)}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="text-muted-foreground">
                  {rule.role === "admin" ? "Admins" : "Editors and admins"}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
