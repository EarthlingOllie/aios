import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ArrowLeft, Loader2, RefreshCw, Trash2 } from "lucide-react";

import {
  DetailPage,
  ErrorBanner,
  formatDate,
  RoleNote,
  Section,
  useLoad,
} from "@/components/crud";
import { ConnectorBadge, goConnect, SourceBadge } from "@/components/Sources";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, describeError, type Connector, type ScopeItem } from "@/lib/api";
import {
  RESTRICTED_WARNING,
  restrictedPicked,
  scopeChanged,
} from "@/lib/connectors";
import { useRefusal } from "@/lib/current-user";
import { syncSummary } from "@/lib/sources";

/**
 * One connector's page (`/data/connectors/:connectorId`): its state and source, and, for admins,
 * Reconnect when its access failed, choosing what it syncs (restricted items need the
 * acknowledgement; GitHub's documentation files on or off), and removing it, keeping or deleting
 * what it synced. Everyone else sees it,
 * with the actions disabled and why.
 */
export function ConnectorPage() {
  const connectorId = Number(useParams().connectorId);
  const {
    data: connector,
    error: loadError,
    reload,
  } = useLoad(useCallback(() => api.getConnector(connectorId), [connectorId]));
  const refusal = useRefusal("sources");
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();

  if (!connector)
    return (
      <DetailPage title="Connector">
        <ErrorBanner message={loadError} />
        {!loadError && (
          <Loader2
            className="text-muted-foreground size-4 animate-spin"
            aria-label="Loading"
          />
        )}
      </DetailPage>
    );

  async function reconnect() {
    setBusy("reconnect");
    setError(undefined);
    try {
      await goConnect(() => api.reconnectConnector(connector!.id));
    } catch (err) {
      setError(describeError(err));
      setBusy(undefined);
    }
  }

  const removing = connector.status === "removing";
  return (
    <div className="h-full overflow-y-auto">
      <DetailPage
        title={
          <span className="flex items-center gap-2">
            {connector.name} <ConnectorBadge connector={connector} />
          </span>
        }
        subtitle={`Connected ${formatDate(connector.created_at)}`}
        actions={
          connector.status === "needs_reauth" && (
            <Button
              size="sm"
              disabled={
                !!refusal || busy !== undefined || !!connector.unavailable
              }
              title={refusal ?? connector.unavailable ?? undefined}
              onClick={() => void reconnect()}
            >
              {busy === "reconnect" ? (
                <Loader2 className="animate-spin" />
              ) : (
                <RefreshCw />
              )}
              Reconnect
            </Button>
          )
        }
      >
        <Link
          to="/data/sources"
          className="text-muted-foreground flex items-center gap-1 text-sm hover:underline"
        >
          <ArrowLeft className="size-4" /> Sources
        </Link>
        <RoleNote refusal={refusal} />
        <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        {connector.unavailable && (
          <p
            role="note"
            className="bg-muted/50 rounded-md border px-3 py-2 text-sm"
          >
            {connector.unavailable} It doesn't sync meanwhile; what it synced
            stays searchable.
          </p>
        )}
        {connector.status === "needs_reauth" && (
          <p
            role="note"
            className="border-destructive/50 text-destructive rounded-md border px-3 py-2 text-sm"
          >
            Its access stopped working, so it has stopped syncing. Reconnect it
            to carry on from where it stopped.
          </p>
        )}
        <SourceSummary connector={connector} />
        {connector.attention && connector.attention.length > 0 && (
          <Section title="Needs attention" label="Needs attention">
            <ul className="divide-y rounded-md border border-amber-500/50">
              {connector.attention.map((item) => (
                <li key={item.id} className="px-3 py-2 text-sm">
                  <span className="font-medium">{item.name}</span>
                  <p className="text-muted-foreground text-xs">{item.reason}</p>
                </li>
              ))}
            </ul>
          </Section>
        )}
        {!removing && (
          <ScopePicker
            connector={connector}
            refusal={refusal}
            onSaved={() => void reload()}
          />
        )}
        {!removing && <Remove connector={connector} refusal={refusal} />}
      </DetailPage>
    </div>
  );
}

function SourceSummary({ connector }: { connector: Connector }) {
  const source = connector.source;
  if (!source) return null;
  return (
    <Section title="Syncing" label="Syncing">
      <div className="flex items-center gap-3 rounded-md border px-3 py-2">
        <div className="min-w-0 flex-1">
          <Link
            to={`/data/sources/${source.id}`}
            className="text-sm hover:underline"
          >
            {source.item_count} {source.item_count === 1 ? "item" : "items"},{" "}
            {source.document_count}{" "}
            {source.document_count === 1 ? "document" : "documents"}
          </Link>
          {source.last_sync && (
            <p className="text-muted-foreground text-xs">
              {syncSummary(source.last_sync)}
            </p>
          )}
          {source.last_sync?.error && (
            <p className="text-destructive text-xs break-words">
              {source.last_sync.error}
            </p>
          )}
        </div>
        <SourceBadge source={source} />
      </div>
    </Section>
  );
}

/** Searching the provider's options and picking what syncs; restricted ones need accepting the
 * warning before saving. */
function ScopePicker({
  connector,
  refusal,
  onSaved,
}: {
  connector: Connector;
  refusal: string | null;
  onSaved: () => void;
}) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<ScopeItem[]>();
  const [picked, setPicked] = useState<string[]>(() =>
    connector.scope.map((i) => i.id),
  );
  const [acknowledged, setAcknowledged] = useState(false);
  const [docs, setDocs] = useState(connector.docs);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const usable =
    !refusal && connector.status === "active" && !connector.unavailable;

  useEffect(() => {
    if (!usable) return;
    let stale = false;
    const timer = setTimeout(() => {
      api
        .connectorScopeOptions(connector.id, query)
        .then((found) => stale || setOptions(found))
        .catch((err) => stale || setError(describeError(err)));
    }, 200);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [connector.id, query, usable]);

  const known = [
    ...connector.scope,
    ...(options ?? []).filter(
      (o) => !connector.scope.some((s) => s.id === o.id),
    ),
  ];
  const restricted = restrictedPicked(known, picked);
  const changed =
    scopeChanged(connector.scope, picked) || docs !== connector.docs;

  function toggle(id: string, on: boolean) {
    setPicked((now) => (on ? [...now, id] : now.filter((x) => x !== id)));
  }

  async function save() {
    setBusy(true);
    setError(undefined);
    try {
      await api.setConnectorScope(
        connector.id,
        picked,
        acknowledged,
        docs ?? undefined,
      );
      setAcknowledged(false);
      onSaved();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  const shown = options ?? connector.scope;
  return (
    <Section title="What it syncs" label="What it syncs">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {connector.scope_hint && (
        <p role="note" className="text-muted-foreground text-sm">
          {connector.scope_hint}
        </p>
      )}
      {usable && (
        <Input
          aria-label="Search what it can sync"
          placeholder="Search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      )}
      {shown.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          {usable ? "Nothing matches." : "Nothing chosen yet."}
        </p>
      ) : (
        <ul className="divide-y rounded-md border" aria-label="Scope options">
          {shown.map((item) => (
            <li
              key={item.id}
              className="flex items-center gap-2 px-3 py-2 text-sm"
            >
              <label className="flex flex-1 items-center gap-2">
                <input
                  type="checkbox"
                  checked={picked.includes(item.id)}
                  disabled={!usable}
                  onChange={(e) => toggle(item.id, e.target.checked)}
                />
                {item.name}
              </label>
              {item.restricted && <Badge variant="secondary">Restricted</Badge>}
            </li>
          ))}
        </ul>
      )}
      {docs !== null && (
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={docs}
            disabled={!usable}
            onChange={(e) => setDocs(e.target.checked)}
          />
          <span className="flex flex-col">
            Also sync README and docs files
            <span className="text-muted-foreground text-xs">
              The README and Markdown files under docs/ on each repository's
              default branch, as documents. Turning it off removes them.
            </span>
          </span>
        </label>
      )}
      {usable && restricted.length > 0 && (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded-md border border-amber-500/50 px-3 py-2 text-sm"
        >
          <p>{RESTRICTED_WARNING}</p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
            />
            I understand
          </label>
        </div>
      )}
      {usable && (
        <div>
          <Button
            size="sm"
            disabled={
              busy || !changed || (restricted.length > 0 && !acknowledged)
            }
            onClick={() => void save()}
          >
            {busy && <Loader2 className="animate-spin" />}
            Save
          </Button>
        </div>
      )}
    </Section>
  );
}

/** Removing the connector: its content is kept as Uploads documents, or deleted. */
function Remove({
  connector,
  refusal,
}: {
  connector: Connector;
  refusal: string | null;
}) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [keep, setKeep] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function remove() {
    setBusy(true);
    setError(undefined);
    try {
      await api.removeConnector(connector.id, keep);
      navigate("/data/sources");
    } catch (err) {
      setError(describeError(err));
      setBusy(false);
    }
  }

  return (
    <Section title="Remove" label="Remove">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {!open ? (
        <div>
          <Button
            size="sm"
            variant="outline"
            disabled={!!refusal}
            title={refusal ?? undefined}
            onClick={() => setOpen(true)}
          >
            <Trash2 />
            Remove connector
          </Button>
        </div>
      ) : (
        <form
          className="flex flex-col gap-2 rounded-md border p-3 text-sm"
          aria-label="Remove connector"
          onSubmit={(e) => {
            e.preventDefault();
            void remove();
          }}
        >
          <p>
            It stops syncing at once, and its access is revoked. What should
            happen to what it synced?
          </p>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="keep"
              checked={keep}
              onChange={() => setKeep(true)}
            />
            Keep it as documents in Uploads
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="keep"
              checked={!keep}
              onChange={() => setKeep(false)}
            />
            Delete it
          </label>
          <div className="flex gap-2">
            <Button
              type="submit"
              size="sm"
              variant="destructive"
              disabled={busy}
            >
              {busy && <Loader2 className="animate-spin" />}
              Remove
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}
    </Section>
  );
}
