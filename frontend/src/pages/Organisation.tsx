import { useState } from "react";
import { Loader2, Pencil } from "lucide-react";

import { ErrorBanner, formatDate, Placeholder, useLoad } from "@/components/crud";
import { RunHistoryExportSection } from "@/components/RunHistoryExport";
import { Tabs, type Tab } from "@/components/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LeaveSection } from "@/components/LeaveOrganisation";
import { InvitesSection } from "@/components/OrganisationInvites";
import { api, describeError, type Me, type Organisation } from "@/lib/api";
import { useCurrentUser, useReloadCurrentUser } from "@/lib/current-user";
import { describeOrganisation, linkName } from "@/lib/organisation";
import { openInOrganisation } from "@/lib/organisation-path";

/**
 * The user's organisation: its name (the creator renames it) above three tabs, as on the agent
 * page: its creator and members (`/organisation`), its invitation links (`/organisation/invitations`,
 * the creator's only), and its settings, the run history export and leaving it
 * (`/organisation/settings`).
 */
export function OrganisationPage() {
  const me = useCurrentUser();
  const reloadMe = useReloadCurrentUser();
  const { data: org, error: loadError, reload } = useLoad(api.getOrganisation);
  const [renaming, setRenaming] = useState(false);

  if (!org) {
    return loadError ? (
      <Placeholder>{loadError}</Placeholder>
    ) : (
      <Placeholder>
        <Loader2 className="size-4 animate-spin" />
      </Placeholder>
    );
  }

  const tabs: Tab[] = [
    {
      id: "members",
      label: "Members",
      count: org.members.length,
      content: <MembersList org={org} me={me} />,
    },
    // Only the creator makes and revokes links, so only they have the tab.
    ...(org.is_creator
      ? [{ id: "invitations", label: "Invitations", content: <InvitesSection key={org.id} /> }]
      : []),
    {
      id: "settings",
      label: "Settings",
      content: (
        <>
          <RunHistoryExportSection key={`export-${org.id}`} />
          <LeaveSection
            org={org}
            onLeft={(landed) => {
              // Into a new personal organisation: its page, at its own link.
              openInOrganisation(landed.slug, "/organisation");
            }}
          />
        </>
      ),
    },
  ];

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
        <header className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold break-words">{org.name}</h1>
            <p className="text-muted-foreground mt-1 text-sm">{describeOrganisation(org, me)}</p>
          </div>
          {org.is_creator && !renaming && (
            <Button variant="outline" size="sm" onClick={() => setRenaming(true)}>
              <Pencil />
              Rename
            </Button>
          )}
        </header>
        {renaming && (
          <RenameForm
            name={org.name}
            onDone={async (renamed) => {
              setRenaming(false);
              if (renamed) {
                // The shell shows the name too.
                reloadMe();
                await reload();
              }
            }}
          />
        )}
        <Tabs label="Organisation details" tabs={tabs} />
      </div>
    </div>
  );
}

function MembersList({ org, me }: { org: Organisation; me: Me | null }) {
  return (
    <ul aria-label="Members" className="divide-y rounded-md border">
      {org.members.map((m) => (
        <li key={m.user.id} className="flex items-center gap-3 px-3 py-2 text-sm">
          <span className="bg-muted size-7 shrink-0 rounded-full" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="truncate font-medium">{m.user.name}</span>
              {m.user.id === me?.id && <Badge variant="secondary">You</Badge>}
              {m.user.id === org.creator?.id && <Badge variant="outline">Creator</Badge>}
            </div>
            <div className="text-muted-foreground truncate text-xs">
              Joined {formatDate(m.joined_at)}
              {m.invite_id && ` through ${linkName(m.invite_id)}`}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function RenameForm({ name, onDone }: { name: string; onDone: (renamed: boolean) => void }) {
  const [value, setValue] = useState(name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      await api.renameOrganisation(value.trim());
      onDone(true);
    } catch (err) {
      setError(describeError(err));
      setSaving(false);
    }
  }

  return (
    <form className="flex flex-col gap-3 rounded-md border p-3" onSubmit={onSubmit}>
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <label htmlFor="organisation-name" className="text-sm font-medium">
        Name
      </label>
      <Input
        id="organisation-name"
        required
        maxLength={255}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        autoFocus
      />
      <p className="text-muted-foreground text-xs">Unique among organisations.</p>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={saving || !value.trim()}>
          Save
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => onDone(false)}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
