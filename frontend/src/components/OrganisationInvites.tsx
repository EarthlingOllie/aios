import { useState } from "react";
import { Check, Copy, Link2, Trash2 } from "lucide-react";

import { ErrorBanner, formatDate, Section, selectClass, useLoad } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type NewInvite } from "@/lib/api";
import { useCurrentUser } from "@/lib/current-user";
import { linkName } from "@/lib/organisation";

// How long a new link lasts, in days: a week by default, a month at most (as the API allows).
const LASTS = [
  { days: 1, label: "A day" },
  { days: 7, label: "A week" },
  { days: 14, label: "Two weeks" },
  { days: 30, label: "A month" },
];

/**
 * The organisation's invitation links, for its creator (the page shows it to no one else): make
 * one and copy it, once, since only its hash is kept; see the open ones, who made each and when
 * it expires; revoke one.
 */
export function InvitesSection() {
  const me = useCurrentUser();
  const { data: invites, error: loadError, reload } = useLoad(api.listInvites);
  const [days, setDays] = useState(7);
  const [made, setMade] = useState<NewInvite>();
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await fn();
    } catch (err) {
      setError(describeError(err));
    } finally {
      await reload();
      setBusy(false);
    }
  }

  return (
    <Section title="Invitation links" label="Invitation links">
      <p className="text-muted-foreground text-sm">
        Anyone with a link can join the organisation until it expires or you revoke it.
      </p>
      <ErrorBanner message={error ?? loadError} onDismiss={() => setError(undefined)} />
      {made ? (
        <div className="flex flex-col gap-2 rounded-md border p-3">
          <label htmlFor="new-invite" className="text-sm font-medium">
            New link
          </label>
          <div className="flex gap-2">
            <Input id="new-invite" readOnly value={made.url} onFocus={(e) => e.target.select()} />
            <Button
              variant="outline"
              onClick={() =>
                navigator.clipboard.writeText(made.url).then(
                  () => setCopied(true),
                  () => setError("Couldn't copy it: select the link and copy it yourself."),
                )
              }
            >
              {copied ? <Check /> : <Copy />}
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">Copy it now: it won't be shown again.</p>
          <div>
            <Button size="sm" variant="ghost" onClick={() => setMade(undefined)}>
              Done
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="invite-lasts" className="text-sm font-medium">
              Lasts
            </label>
            <select
              id="invite-lasts"
              className={selectClass}
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
            >
              {LASTS.map((l) => (
                <option key={l.days} value={l.days}>
                  {l.label}
                </option>
              ))}
            </select>
          </div>
          <Button
            disabled={busy}
            onClick={() =>
              act(async () => {
                setCopied(false);
                setMade(await api.makeInvite(days));
              })
            }
          >
            <Link2 />
            Make a link
          </Button>
        </div>
      )}
      {invites &&
        (invites.length === 0 ? (
          <p className="text-muted-foreground text-sm">No open links.</p>
        ) : (
          <ul aria-label="Open links" className="divide-y rounded-md border">
            {invites.map((invite) => (
              <li key={invite.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{linkName(invite.id)}</div>
                  <div className="text-muted-foreground truncate text-xs">
                    Made by{" "}
                    {invite.creator
                      ? invite.creator.id === me?.id
                        ? "you"
                        : invite.creator.name
                      : "a deleted account"}{" "}
                    on {formatDate(invite.created_at)} · Expires {formatDate(invite.expires_at)}
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() => {
                    if (!window.confirm(`Revoke ${linkName(invite.id)}? Nobody can join through it after.`)) {
                      return;
                    }
                    act(() => api.revokeInvite(invite.id));
                  }}
                >
                  <Trash2 />
                  Revoke
                </Button>
              </li>
            ))}
          </ul>
        ))}
    </Section>
  );
}
