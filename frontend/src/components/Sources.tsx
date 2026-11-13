import { useCallback, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { Check, Copy, Globe, Inbox, Loader2, Mail, Plug, Plus, Upload, Webhook } from "lucide-react";

import { ErrorBanner, Field, formatDate, RoleNote, useLoad } from "@/components/crud";
import { Notice } from "@/components/Notice";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  describeError,
  type Connector,
  type ConnectorProvider,
  type Source,
  type SourceDetail,
  type SourceHealth,
} from "@/lib/api";
import { connectorState } from "@/lib/connectors";
import { useRefusal } from "@/lib/current-user";
import {
  intervalLabel,
  isPushed,
  parseSenders,
  sourceHealthLabel,
  sourceKindLabel,
  sourceStatusLabel,
  syncSummary,
} from "@/lib/sources";

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
          Uploads; web pages sync on their own schedule, and webhooks and email addresses keep what's sent to them.
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
                  {connector?.attention && connector.attention.length > 0 && (
                    <p className="text-xs text-amber-700 dark:text-amber-400">
                      Needs attention: {connector.attention.map((i) => i.name).join(", ")}
                    </p>
                  )}
                </div>
                {connector ? (
                  <span className="flex items-center gap-2">
                    {connector.source && connector.status === "active" && !connector.unavailable && (
                      <SourceBadge source={connector.source} />
                    )}
                    <ConnectorBadge connector={connector} />
                  </span>
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
  const Icon = { upload: Upload, url: Globe, webhook: Webhook, email: Mail, connector: Inbox }[source.kind];
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
          {source.last_sync &&
            ` · ${isPushed(source.kind) ? "last received" : "last synced"} ${formatDate(source.last_sync.started_at)}`}
        </p>
        {source.last_sync && source.last_sync.status !== "running" && source.kind !== "upload" && (
          <p className="text-muted-foreground text-xs">{syncSummary(source.last_sync)}</p>
        )}
      </div>
      <SourceBadge source={source} />
    </li>
  );
}

type AddKind = "url" | "webhook" | "email" | "connector";

const KIND_CHOICE: Record<AddKind, string> = {
  url: "Web page",
  webhook: "Webhook",
  email: "Email",
  connector: "Test provider",
};

/** What a webhook or email source answered with once: its secret and URL, or its address. */
export function ShownOnce({ source, onDone }: { source: SourceDetail; onDone: () => void }) {
  const values: [string, string][] = [];
  if (source.webhook_url && source.webhook_secret) {
    values.push(["Webhook URL", source.webhook_url], ["Signing secret", source.webhook_secret]);
  }
  if (source.email_address) values.push(["Address", source.email_address]);
  return (
    <div className="flex flex-col gap-2 rounded-md border p-3" role="region" aria-label="Shown once">
      {values.map(([label, value]) => (
        <CopyField key={label} label={label} value={value} />
      ))}
      <p className="text-muted-foreground text-xs">
        {source.email_address
          ? "Copy the address now: it won't be shown again. Forward mail from an allowed sender to it."
          : "Copy the secret now: it won't be shown again. Sign each call with it."}
      </p>
      <div>
        <Button size="sm" variant="ghost" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const id = `shown-${label.toLowerCase().replace(/\s+/g, "-")}`;
  return (
    <Field label={label} htmlFor={id}>
      <div className="flex gap-2">
        <Input id={id} readOnly value={value} onFocus={(e) => e.target.select()} />
        <Button
          type="button"
          variant="outline"
          onClick={() => navigator.clipboard.writeText(value).then(() => setCopied(true), () => undefined)}
        >
          {copied ? <Check /> : <Copy />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </Field>
  );
}

/**
 * Adding a source: a web page (fetched on its interval), a webhook (its URL and secret shown once),
 * an email address (shown once; only mail from allowed senders is kept), or the development test
 * provider where it's offered. A kind this deployment doesn't offer is shown disabled, with why.
 */
function AddSource({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const providers = useLoad(useCallback(() => api.listSourceProviders(), []));
  const kinds = useLoad(useCallback(() => api.listSourceKinds(), []));
  const [kind, setKind] = useState<AddKind>("url");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [senders, setSenders] = useState("");
  const [minutes, setMinutes] = useState("15");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [made, setMade] = useState<SourceDetail>();
  const offered = providers.data?.find((p) => p.unavailable === null);
  const why = (k: AddKind): string | null =>
    k === "connector"
      ? offered
        ? null
        : "No provider can be added here."
      : (kinds.data?.find((o) => o.kind === k)?.unavailable ?? null);
  const choices = (["url", "webhook", "email", "connector"] as AddKind[]).filter((k) => k !== "connector" || offered);
  const unavailable = why(kind);
  const syncs = kind === "url" || kind === "connector";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const source = await api.createSource({
        name: name.trim(),
        kind,
        ...(kind === "connector" && offered ? { provider: offered.provider } : {}),
        ...(kind === "url" ? { url: url.trim() } : {}),
        ...(kind === "email" ? { allowed_senders: parseSenders(senders) } : {}),
        ...(syncs ? { interval_seconds: Math.round(Number(minutes) * 60) } : {}),
      });
      if (isPushed(source.kind)) setMade(source);
      else onDone();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  if (made) return <ShownOnce source={made} onDone={onDone} />;

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 rounded-md border p-3" aria-label="Add source">
      <ErrorBanner message={error ?? providers.error ?? kinds.error} onDismiss={() => setError(undefined)} />
      <Field label="Kind" htmlFor="source-kind">
        <select
          id="source-kind"
          className="border-input bg-background h-9 rounded-md border px-2 text-sm"
          value={kind}
          onChange={(e) => setKind(e.target.value as AddKind)}
        >
          {choices.map((k) => (
            <option key={k} value={k} disabled={why(k) !== null}>
              {KIND_CHOICE[k]}
              {why(k) !== null ? " (unavailable)" : ""}
            </option>
          ))}
        </select>
      </Field>
      {(["email", "webhook", "url"] as AddKind[])
        .filter((k) => why(k) !== null)
        .map((k) => (
          <p key={k} className="text-muted-foreground text-sm">
            {KIND_CHOICE[k]}: {why(k)}
          </p>
        ))}
      <Field label="Name" htmlFor="source-name">
        <Input id="source-name" value={name} onChange={(e) => setName(e.target.value)} required />
      </Field>
      {kind === "url" && (
        <Field label="URL" htmlFor="source-url" hint="A public web page, over HTTP or HTTPS.">
          <Input
            id="source-url"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/prices"
            required
          />
        </Field>
      )}
      {kind === "email" && (
        <Field
          label="Allowed senders"
          htmlFor="source-senders"
          hint="One per line: an address, or a whole domain as @example.com. Mail from anyone else is dropped."
        >
          <Textarea id="source-senders" value={senders} onChange={(e) => setSenders(e.target.value)} rows={3} />
        </Field>
      )}
      {syncs && (
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
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || unavailable !== null || !name.trim()}>
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
