import { useState } from "react";
import { LogOut } from "lucide-react";

import { ErrorBanner, Section } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { api, describeError, type Organisation } from "@/lib/api";
import { leaveRefusal } from "@/lib/organisation";

/**
 * Leaving the organisation: what the member made stays in it, and they land in a new personal
 * organisation of their own. Refused, with why, for the last member and for the creator while
 * others remain (`leaveRefusal`, as the API refuses it).
 */
export function LeaveSection({ org, onLeft }: { org: Organisation; onLeft: (landed: Organisation) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const refusal = leaveRefusal(org);

  async function onLeave() {
    if (!window.confirm(`Leave ${org.name}? You'll no longer see anything in it.`)) return;
    setBusy(true);
    setError(undefined);
    try {
      onLeft(await api.leaveOrganisation());
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Leave" label="Leave">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <p className="text-muted-foreground text-sm">
        {refusal ??
          "What you made stays in the organisation, with your name on it. You'll leave its chats, and land in a new personal organisation of your own."}
      </p>
      <div>
        <Button variant="outline" disabled={busy || refusal !== null} onClick={onLeave}>
          <LogOut />
          Leave organisation
        </Button>
      </div>
    </Section>
  );
}
