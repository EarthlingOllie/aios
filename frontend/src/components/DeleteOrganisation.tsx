import { useState } from "react";
import { Trash2, Undo2 } from "lucide-react";

import { ErrorBanner, formatDate, Section } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type Organisation } from "@/lib/api";
import { confirmsDeletion } from "@/lib/organisation";

/**
 * Deleting the organisation: its creator types its name, and it's deleted after a grace period,
 * which every member is told about (and a banner on every page dates); until then the creator can
 * cancel. Another member sees only whether it's scheduled. `onChanged` gets the organisation as
 * it is after scheduling or cancelling.
 */
export function DeleteSection({ org, onChanged }: { org: Organisation; onChanged: (org: Organisation) => void }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function act(call: () => Promise<Organisation>) {
    setBusy(true);
    setError(undefined);
    try {
      onChanged(await call());
      setTyped("");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  const deletion = org.deletion;
  const scheduledBy = deletion?.scheduled_by ? deletion.scheduled_by.name : "Its creator";
  return (
    <Section title="Delete organisation" label="Delete organisation">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {deletion ? (
        <p className="text-sm">
          {scheduledBy} scheduled it for deletion on {formatDate(deletion.scheduled_at)}. It will be deleted, with
          everything in it, on {formatDate(deletion.deletes_at)}
          {org.is_creator ? ", unless you cancel before then." : "."}
        </p>
      ) : (
        <p className="text-muted-foreground text-sm">
          {org.is_creator
            ? "It's deleted, with everything in it and its data store, after a grace period, in which you can cancel. Every member is told."
            : "Only its creator can delete it."}
        </p>
      )}
      {org.is_creator &&
        (deletion ? (
          <div>
            <Button variant="outline" disabled={busy} onClick={() => act(api.cancelOrganisationDeletion)}>
              <Undo2 />
              Cancel deletion
            </Button>
          </div>
        ) : (
          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (confirmsDeletion(typed, org.name)) act(() => api.scheduleOrganisationDeletion(typed.trim()));
            }}
          >
            <label htmlFor="delete-organisation-name" className="text-sm font-medium">
              Type {org.name} to confirm
            </label>
            <Input
              id="delete-organisation-name"
              autoComplete="off"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
            <div>
              <Button type="submit" variant="destructive" disabled={busy || !confirmsDeletion(typed, org.name)}>
                <Trash2 />
                Delete organisation
              </Button>
            </div>
          </form>
        ))}
    </Section>
  );
}
