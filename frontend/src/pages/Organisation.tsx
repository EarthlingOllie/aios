import { useState } from "react";
import { Loader2, Pencil } from "lucide-react";

import { ErrorBanner, formatDate, Placeholder, RoleNote, selectClass, useLoad } from "@/components/crud";
import { ApprovalRulesSection } from "@/components/ApprovalRules";
import { AuditLogSection } from "@/components/AuditLog";
import { FlaggedSection } from "@/components/FlaggedDocuments";
import { Tabs, type Tab } from "@/components/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DeleteSection } from "@/components/DeleteOrganisation";
import { LeaveSection } from "@/components/LeaveOrganisation";
import { InvitesSection } from "@/components/OrganisationInvites";
import { api, describeError, type Me, type Organisation, type Role } from "@/lib/api";
import { useCurrentUser, useReloadCurrentUser } from "@/lib/current-user";
import { describeOrganisation, linkName } from "@/lib/organisation";
import { landingPath, recentOrganisations } from "@/lib/organisation-path";
import { refusal, ROLE_DESCRIPTIONS, roleLabel } from "@/lib/permissions";

const ROLES: Role[] = ["admin", "editor", "participant"];

/**
 * The user's organisation: its name (admins rename it) above three tabs, as on the agent
 * page: its creator and members with their roles, which admins change (`/organisation`), its
 * invitation links (`/organisation/invitations`, admins' only), its settings: the flagged list
 * (admins' only), the approval rules, leaving it and deleting it (`/organisation/settings`), and its audit log
 * (`/organisation/audit-log`, admins' only).
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
      content: <MembersList org={org} me={me} onChanged={reload} />,
    },
    // Only admins make and revoke links, so only they have the tab.
    ...(org.role === "admin"
      ? [{ id: "invitations", label: "Invitations", content: <InvitesSection key={org.id} /> }]
      : []),
    {
      id: "settings",
      label: "Settings",
      content: (
        <>
          {/* Only admins see flagged content and release it. */}
          {org.role === "admin" && <FlaggedSection key={`flagged-${org.id}`} />}
          <ApprovalRulesSection key={`rules-${org.id}`} />
          <LeaveSection
            org={org}
            onLeft={(after) => {
              // Into the organisation used most recently, or the welcome page if none is left: a
              // new page load, as the organisation is the router's basename.
              window.location.assign(landingPath(after.organisations, recentOrganisations()));
            }}
          />
          <DeleteSection
            key={`delete-${org.id}`}
            org={org}
            onChanged={async () => {
              // The shell's banner shows the date too.
              reloadMe();
              await reload();
            }}
          />
        </>
      ),
    },
    // Only admins read the audit log.
    ...(org.role === "admin"
      ? [{ id: "audit-log", label: "Audit log", content: <AuditLogSection key={org.id} org={org} /> }]
      : []),
  ];

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
        <header className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold break-words">{org.name}</h1>
            <p className="text-muted-foreground mt-1 text-sm">{describeOrganisation(org, me)}</p>
          </div>
          {org.role === "admin" && !renaming && (
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

function MembersList({
  org,
  me,
  onChanged,
}: {
  org: Organisation;
  me: Me | null;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const admin = org.role === "admin";

  async function change(userId: number, role: Role) {
    setBusy(true);
    setError(undefined);
    try {
      await api.changeRole(userId, role);
      await onChanged();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <RoleNote refusal={refusal(org.role, "run_organisation")} />
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
            {admin ? (
              <select
                aria-label={`${m.user.name}'s role`}
                className={selectClass + " w-auto"}
                value={m.role}
                disabled={busy}
                onChange={(e) => change(m.user.id, e.target.value as Role)}
              >
                {ROLES.map((role) => (
                  <option key={role} value={role} title={ROLE_DESCRIPTIONS[role]}>
                    {roleLabel(role)}
                  </option>
                ))}
              </select>
            ) : (
              <Badge variant="outline" aria-label={`${m.user.name}'s role`}>
                {roleLabel(m.role)}
              </Badge>
            )}
          </li>
        ))}
      </ul>
    </div>
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
