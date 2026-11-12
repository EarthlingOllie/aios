import { useCallback, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { Inbox, Loader2, Plug, Plus, Upload } from "lucide-react";

import { ErrorBanner, Field, formatDate, RoleNote, useLoad } from "@/components/crud";
import { Notice } from "@/components/Notice";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type Connector, type ConnectorProvider, type Source, type SourceHealth } from "@/lib/api";
import { connectorState } from "@/lib/connectors";
import { useRefusal } from "@/lib/current-user";
import { intervalLabel, sourceHealthLabel, sourceKindLabel, sourceStatusLabel, syncSummary } from "@/lib/sources";

export const statusVariant = {
  active: "outline",
  paused: "secondary",
  failing: "destructive",
  needs_reauth: "destructive",
  removing: "secondary",
} as const;

export const healthVariant: Record<SourceHealth, "outline" | "secondary" | "destructive"> = {
  healthy: "outline",
  delayed: "secondary",
  failing: "destructive",
  needs_reconnecting: "destructive",
};

/** The source's health, as a badge (its status instead while it's paused or being removed). */
export function SourceBadge({ source }: { source: Pick<Source, "status" | "health"> }) {
  if (source.status === "paused" || source.status === "removing")
    return <Badge variant={statusVariant[source.status]}>{sourceStatusLabel(source.status)}</Badge>;
  return (
    <Badge variant={healthVariant[source.health]} aria-label={`Health: ${sourceHealthLabel(source.health)}`}>
      {sourceHealthLabel(source.health)}
    </Badge>
  );
}

/**
 * The Data page's Sources: what brings content into the organisation's store, with each one's
 * health and last sync, for every member to see. Admins add sources that sync; editors and
 * participants see the action disabled, with why. Uploads is always on.
 */
export function SourcesSection() {
  const list = useLoad(useCallback(() => api.listSources(), []));
  const [adding, setAdding] = useState(false);
  const refusal = useRefusal("sources");
  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <p className="text-muted-foreground flex-1 text-sm">
          Where the organisation's documents and synced items come from. Everything members upload belongs to
          Uploads; other sources sync on their own schedule.
        </p>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setAdding(true)}
          disabled={!!refusal || adding}
          title={refusal ?? undefined}
        >
          <Plus />
          Add source
        </Button>
      </div>
      <RoleNote refusal={refusal} />
      {adding && (
        <AddSource
          onDone={() => {
            setAdding(false);
            void list.reload();
          }}
          onCancel={() => setAdding(false)}
        />
      )}
      {list.error && (
        <Notice label="Sources" error className="px-3 py-2">
          {list.error}
        </Notice>
      )}
      {!list.data && !list.error && (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      )}
      {list.data && list.data.length > 0 && (
        <ul className="divide-y rounded-md border" aria-label="Sources">
          {list.data.map((source) => (
            <SourceRow key={source.id} source={source} />
          ))}
        </ul>
      )}
      <ConnectorsSection />
    </section>
  );
}

/** The connector's state, as a badge: "Reconnect" when its access failed. */
export function ConnectorBadge({ connector }: { connector: Pick<Connector, "status" | "unavailable"> }) {
  const state = connectorState(connector);
  return <Badge variant={state.attention ? "destructive" : "outline"}>{state.label}</Badge>;
}

/** Sends the browser to the provider to authorise aios; it comes back to the connector's page. */
export async function goConnect(start: () => Promise<{ redirect_url: string }>) {
  const { redirect_url } = await start();
  window.location.assign(redirect_url);
}

/**
 * The systems this deployment can connect (Slack, Google Drive, GitHub), with why any can't be,
 * and the organisation's connectors. Admins connect; everyone else sees Connect disabled, with why.
 */
function ConnectorsSection() {
  const providers = useLoad(useCallback(() => api.listConnectorProviders(), []));
  const connectors = useLoad(useCallback(() => api.listConnectors(), []));
  const refusal = useRefusal("sources");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState<string>();
  const byId = new Map((connectors.data ?? []).map((c) => [c.id, c]));

  async function connect(provider: ConnectorProvider) {
    setBusy(provider.provider);
    setError(undefined);
    try {
      await goConnect(() => api.startConnecting(provider.provider));
    } catch (err) {
      setError(describeError(err));
      setBusy(undefined);
    }
  }

  return (
    <section className="flex flex-col gap-2" aria-label="Connectors">
      <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">Connectors</h3>
      <p className="text-muted-foreground text-sm">
        Connect a system once, then choose exactly what it syncs. Everything synced is searchable by everyone in the
        organisation.
      </p>
      <ErrorBanner message={error ?? providers.error ?? connectors.error} onDismiss={() => setError(undefined)} />
      {providers.data && (
        <ul className="divide-y rounded-md border" aria-label="Connectors">
          {providers.data.map((provider) => {
            const connector = provider.connector_id === null ? undefined : byId.get(provider.connector_id);
            return (
              <li key={provider.provider} className="flex items-center gap-3 px-3 py-2">
                <Plug className="text-muted-foreground size-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  {connector ? (
                    <Link to={`/data/connectors/${connector.id}`} className="block truncate text-sm hover:underline">
                      {provider.title}
                    </Link>
                  ) : (
                    <p className="text-sm">{provider.title}</p>
                  )}
                  <p className="text-muted-foreground text-xs">
                    {connector
                      ? connector.scope.length === 0
                        ? "Choose what it syncs."
                        : `Syncs ${connector.scope.map((i) => i.name).join(", ")}`
                      : (provider.unavailable ?? "Not connected.")}
                  </p>
                </div>
                {connector ? (
                  <ConnectorBadge connector={connector} />
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!!refusal || !!provider.unavailable || busy !== undefined}
                    title={refusal ?? provider.unavailable ?? undefined}
                    onClick={() => void connect(provider)}
                  >
                    {busy === provider.provider && <Loader2 className="animate-spin" />}
                    Connect
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function SourceRow({ source }: { source: Source }) {
  const Icon = source.kind === "upload" ? Upload : Inbox;
  const count = source.document_count;
  const every = intervalLabel(source.interval_seconds);
  return (
    <li className="flex items-center gap-3 px-3 py-2">
      <Icon className="text-muted-foreground size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <Link to={`/data/sources/${source.id}`} className="block truncate text-sm hover:underline">
          {source.name}
        </Link>
        <p className="text-muted-foreground text-xs">
          {sourceKindLabel(source.kind)} · {count} {count === 1 ? "document" : "documents"}
          {source.kind !== "upload" && ` · ${source.item_count} ${source.item_count === 1 ? "item" : "items"}`}
          {every && ` · syncs ${every}`}
          {source.last_sync && ` · last synced ${formatDate(source.last_sync.started_at)}`}
        </p>
        {source.last_sync && source.last_sync.status !== "running" && source.kind !== "upload" && (
          <p className="text-muted-foreground text-xs">{syncSummary(source.last_sync)}</p>
        )}
      </div>
      <SourceBadge source={source} />
    </li>
  );
}

/** Adding a source that syncs: today only the development test provider, where it's offered. */
function AddSource({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const providers = useLoad(useCallback(() => api.listSourceProviders(), []));
  const [name, setName] = useState("");
  const [minutes, setMinutes] = useState("15");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const offered = providers.data?.find((p) => p.unavailable === null);
  const unavailable = providers.data && !offered ? providers.data.map((p) => p.unavailable).join(" ") : null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!offered) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.createSource({
        name: name.trim(),
        provider: offered.provider,
        interval_seconds: Math.round(Number(minutes) * 60),
      });
      onDone();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 rounded-md border p-3" aria-label="Add source">
      <ErrorBanner message={error ?? providers.error} onDismiss={() => setError(undefined)} />
      {unavailable && <p className="text-muted-foreground text-sm">{unavailable || "No source can be added here."}</p>}
      <Field label="Name" htmlFor="source-name">
        <Input id="source-name" value={name} onChange={(e) => setName(e.target.value)} required />
      </Field>
      <Field label="Sync every (minutes)" htmlFor="source-interval" hint="At least every 5 minutes.">
        <Input
          id="source-interval"
          type="number"
          min={1}
          value={minutes}
          onChange={(e) => setMinutes(e.target.value)}
          required
        />
      </Field>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !offered || !name.trim()}>
          {busy && <Loader2 className="animate-spin" />}
          Add
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
