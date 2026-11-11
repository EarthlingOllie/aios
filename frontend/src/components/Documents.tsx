import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Download, FileText, Loader2, Trash2, Upload, X } from "lucide-react";
import { useSearchParams } from "react-router";

import { formatDate, RoleNote, selectClass, useLoad, usePoll } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataStoreNotice } from "@/components/DataStoreNotice";
import { Notice } from "@/components/Notice";
import {
  api,
  ApiError,
  describeError,
  organisationApi,
  type DataStoreStatus,
  type DataStoreUsage,
  type DocumentInfo,
} from "@/lib/api";
import { fullWarnings, uploadsRefused, useDataStoreStatus } from "@/lib/data-store";
import { formatBytes } from "@/lib/format-bytes";
import { useRefusal } from "@/lib/current-user";
import { personName } from "@/lib/provenance";
import { documentsFrom } from "@/lib/sources";
import { cn } from "@/lib/utils";
import { verdictView } from "@/lib/verdicts";

const statusVariant = {
  pending: "secondary",
  processing: "secondary",
  ready: "outline",
  failed: "destructive",
} as const;

/**
 * The organisation's documents, read by the views that show them (`useDocumentsList`) and again while
 * any is still being ingested, plus upload and delete. Held once, by `DocumentsProvider`.
 */
function useDocuments(
  storeStatus: DataStoreStatus | undefined,
  usage: DataStoreUsage | undefined,
  recheckStore: () => void,
) {
  // Undefined until the first read answers, so a list still loading isn't taken for an empty one.
  const [docs, setDocs] = useState<DocumentInfo[]>();
  // What to say about the last read of the list failing (e.g. the store is busy); the list
  // keeps its last rows meanwhile.
  const [listError, setListError] = useState<string>();
  // Whether that failure may pass on its own: a refusal (an expired session, a store that isn't
  // ready) answers the same however often it's asked, so it isn't asked again.
  const [listRetrying, setListRetrying] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const ready = storeStatus === "ready";

  // Never rejects: a failed read keeps the last list, and the poll below tries again if the
  // failure is transient. Only the latest read may update state: a slower, older answer (a poll
  // sent before a delete, say) is dropped.
  const latest = useRef(0);
  const refresh = useCallback(() => {
    const request = ++latest.current;
    return api.listDocuments().then(
      (list) => {
        if (request !== latest.current) return;
        setDocs(list);
        setListError(undefined);
        setListRetrying(false);
      },
      (err) => {
        if (request !== latest.current) return;
        // A network error, or the API restarting (502, 504) or busy (503: every connection the
        // store allows is in use), which a moment's wait frees.
        const transient = !(err instanceof ApiError) || [502, 503, 504].includes(err.status);
        setListRetrying(transient);
        const retry = transient ? " Trying again…" : "";
        setListError(
          err instanceof ApiError && err.status === 503
            ? "The organisation's data store is busy. Trying again…"
            : `Couldn't load your documents (${describeError(err)}).${retry}`,
        );
      },
    );
  }, []);

  // Poll while anything is still being ingested by the worker, or the last read failed for a
  // reason that may pass, but not after one that won't (however much is ingesting); each read
  // waits for the last to answer, so a slow store doesn't pile them up.
  const ingesting = Boolean(docs?.some((d) => d.status === "pending" || d.status === "processing"));
  usePoll(ready && (listError ? listRetrying : ingesting), refresh, 2000);

  async function upload(files: File[]) {
    if (!files.length) return;
    setUploading(true);
    setErrors([]);
    const failed: string[] = [];
    try {
      for (const file of files) {
        try {
          await api.uploadDocument(file);
        } catch (err) {
          // Each is tried, and each refusal said: one that doesn't fit leaves room for a smaller.
          failed.push(`${file.name}: ${describeError(err)}`);
        }
      }
    } finally {
      setErrors(failed);
      setUploading(false);
      // The store's usage, which uploads change (and which may now stop them).
      recheckStore();
      await refresh();
    }
  }

  async function remove(doc: DocumentInfo) {
    setErrors([]);
    try {
      await api.deleteDocument(doc.id);
      // The room it made, which may let uploads in again.
      recheckStore();
    } catch (err) {
      // Already gone (deleted twice before the list caught up, or from another tab): that's
      // what was asked for.
      if (err instanceof ApiError && err.status === 404) return;
      setErrors([`Couldn't delete ${doc.filename}: ${describeError(err)}`]);
    } finally {
      await refresh();
    }
  }

  return {
    docs,
    /** Reads the list again; never rejects. */
    refresh,
    /** The store is ready but its list hasn't been read yet (and no read has failed). */
    loading: ready && docs === undefined && !listError,
    // A store that isn't ready says so itself; a list refused meanwhile adds nothing.
    listError: ready ? listError : undefined,
    listRetrying,
    uploading,
    errors,
    dismissErrors: () => setErrors([]),
    upload,
    remove,
    /** Uploads wait for the store to be ready, and for its status to be known. */
    canUpload: ready && !uploadsRefused(usage),
    /** What uploads are refused for, at either cap; empty below both. */
    fullWarnings: ready ? fullWarnings(usage) : [],
  };
}

type DocumentsState = ReturnType<typeof useDocuments>;

type SharedDocuments = {
  store: ReturnType<typeof useDataStoreStatus>;
  documents: DocumentsState;
};

const DocumentsContext = createContext<SharedDocuments | null>(null);

/**
 * The store's status and the organisation's documents, held above the pages: the Chats section's panel,
 * the Data page and the Search page share one status, the panel and the Data page read one list, and an
 * upload still in flight keeps its progress and errors when you switch tabs or pages.
 */
export function DocumentsProvider({ children }: { children: ReactNode }) {
  const store = useDataStoreStatus();
  const documents = useDocuments(store.status, store.usage, store.recheck);
  return <DocumentsContext value={{ store, documents }}>{children}</DocumentsContext>;
}

function useShared() {
  const shared = useContext(DocumentsContext);
  if (!shared) throw new Error("The documents hooks need a DocumentsProvider above them");
  return shared;
}

/**
 * The organisation's data store's status (and why it couldn't be read, while it keeps trying), from the
 * `DocumentsProvider` above. A store last seen ready is asked about again when the caller
 * mounts, as nothing polls it once it's ready.
 */
export function useDataStore() {
  const { status, usage, error, recheck } = useShared().store;
  // Only on mount, and only about a ready store: one still being asked about, or not ready (and
  // so still polled), needs nothing more.
  const recheckReady = useRef(status === "ready" ? recheck : undefined);
  useEffect(() => recheckReady.current?.(), []);
  return { status, usage, error, recheck };
}

/**
 * The store's status and the documents, from the `DocumentsProvider` above. The list is read
 * again when the caller mounts (and when the store becomes ready), so a page or panel opened
 * later shows documents added or deleted elsewhere since.
 */
function useDocumentsList() {
  const { status: storeStatus, error: storeError } = useDataStore();
  const { documents } = useShared();
  const { refresh } = documents;
  const ready = storeStatus === "ready";
  // Not before the store is ready: the API refuses to list them until then.
  useEffect(() => {
    if (ready) refresh();
  }, [ready, refresh]);
  return { storeStatus, storeError, documents };
}

/** The Upload button, with the file picker it opens: disabled, saying why, for a role that
 * can't upload. */
function UploadButton({ documents }: { documents: DocumentsState }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const refusal = useRefusal("documents");
  return (
    <>
      <Button
        size="sm"
        variant="outline"
        title={refusal ?? undefined}
        // One batch at a time, from here or the other view, so they can't trample each other's
        // progress and errors.
        disabled={!documents.canUpload || documents.uploading || !!refusal}
        onClick={() => inputRef.current?.click()}
      >
        {documents.uploading ? <Loader2 className="animate-spin" /> : <Upload />}
        Upload
      </Button>
      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        accept=".pdf,.docx,.pptx,.xlsx,.html,.md,.txt,.csv,.png,.jpg,.jpeg"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          // Cleared straight away, so picking the same file again still uploads it.
          e.target.value = "";
          documents.upload(files);
        }}
      />
    </>
  );
}

/**
 * What's wrong, if anything: a store that isn't ready (or whose status can't be read), a list
 * that couldn't be read, or uploads and deletes that failed.
 */
function DocumentsNotices({
  storeStatus,
  storeError,
  documents,
  className,
}: {
  storeStatus: DataStoreStatus | undefined;
  storeError: string | undefined;
  documents: DocumentsState;
  className: string;
}) {
  return (
    <>
      <DataStoreNotice
        status={storeStatus}
        error={storeError}
        action="upload documents"
        className={className}
      />
      {documents.fullWarnings.length > 0 && (
        <Notice label="Storage" error className={className}>
          {documents.fullWarnings.join(" ")}
        </Notice>
      )}
      {documents.listError && (
        <Notice label="Documents list" busy={documents.listRetrying} className={className}>
          {documents.listError}
        </Notice>
      )}
      {documents.errors.length > 0 && (
        <div
          role="alert"
          className={cn(
            "border-destructive/50 text-destructive flex items-start gap-2 rounded-md border",
            "text-sm",
            className,
          )}
        >
          <ul className="min-w-0 flex-1 space-y-0.5">
            {documents.errors.map((msg, i) => (
              <li key={i} className="break-words">
                {msg}
              </li>
            ))}
          </ul>
          <button
            className="hover:text-foreground shrink-0"
            onClick={documents.dismissErrors}
            aria-label="Dismiss errors"
          >
            <X className="size-4" />
          </button>
        </div>
      )}
    </>
  );
}

const statusLabel = {
  pending: "Waiting",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
} as const;

/** The document's Jev verdict (`lib/verdicts`), with what it means as its tooltip. */
function VerdictBadge({ doc }: { doc: DocumentInfo }) {
  const view = verdictView(doc.verdict, doc.verdict_reason);
  return (
    <Badge variant={view.variant} title={view.detail} aria-label={`Jev: ${view.label}`}>
      {view.label}
    </Badge>
  );
}

function DocumentsLoading() {
  return (
    <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading documents" />
  );
}

/** Whether to say there are no documents: only once a read has answered with none. */
function isEmpty(documents: DocumentsState) {
  return documents.docs?.length === 0 && documents.canUpload && !documents.listError;
}

/** The chat's documents panel: a compact list beside the conversation it grounds. */
export function Documents() {
  const { storeStatus, storeError, documents } = useDocumentsList();
  const { docs } = documents;
  const refusal = useRefusal("documents");

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Documents</h2>
        <UploadButton documents={documents} />
      </div>
      <DocumentsNotices
        storeStatus={storeStatus}
        storeError={storeError}
        documents={documents}
        className="px-2 py-1.5"
      />
      {documents.loading && <DocumentsLoading />}
      <ul className="flex-1 space-y-1 overflow-y-auto">
        {isEmpty(documents) && (
          <li className="text-muted-foreground text-sm">Upload files to search them in chat.</li>
        )}
        {docs?.map((doc) => (
          <li
            key={doc.id}
            className="group hover:bg-accent flex items-center gap-2 rounded-md px-2 py-1.5 text-sm"
            title={doc.error ?? undefined}
          >
            <FileText className="text-muted-foreground size-4 shrink-0" />
            <span className="min-w-0 flex-1 truncate">{doc.filename}</span>
            <Badge variant={statusVariant[doc.status]}>
              {doc.status === "ready" ? `${doc.chunk_count} chunks` : doc.status}
            </Badge>
            {doc.status === "ready" && doc.verdict !== "clean" && <VerdictBadge doc={doc} />}
            {!refusal && (
              <button
                className="text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100"
                onClick={() => documents.remove(doc)}
                aria-label={`Delete ${doc.filename}`}
              >
                <Trash2 className="size-4" />
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Which source's documents the Documents tab shows, kept in the URL as `?source=<id>` (all of
 * them without it), and the sources to choose from: every source the store has, so one with no
 * documents yet can be chosen too.
 */
function useSourceFilter(ready: boolean) {
  const [params, setParams] = useSearchParams();
  const raw = params.get("source");
  const chosen = raw && /^\d+$/.test(raw) ? Number(raw) : undefined;
  const sources = useLoad(
    useCallback(() => (ready ? api.listSources() : Promise.resolve([])), [ready]),
  );
  const choose = (id: number | undefined) =>
    setParams(
      (p) => {
        const next = new URLSearchParams(p);
        if (id === undefined) next.delete("source");
        else next.set("source", String(id));
        return next;
      },
      { replace: true },
    );
  return { chosen, sources: sources.data ?? [], choose };
}

/**
 * The Data page's Documents: every document in the organisation's store with the source it
 * belongs to, its ingest status (and why one failed), chunk count, size, and when and by whom it
 * was uploaded, to download or delete; filtered by source. Uploads from the Chats section's
 * panel land in the same list.
 */
export function DocumentsSection() {
  const { storeStatus, storeError, documents } = useDocumentsList();
  const filter = useSourceFilter(storeStatus === "ready");
  const docs = documents.docs && documentsFrom(documents.docs, filter.chosen);
  const refusal = useRefusal("documents");

  const onDelete = (doc: DocumentInfo) => {
    if (!window.confirm(`Delete ${doc.filename}? It will no longer be found by search.`)) return;
    documents.remove(doc);
  };

  return (
    <>
      <div className="flex items-center justify-between gap-2">
        <p className="text-muted-foreground text-sm">
          Files your agents and the chat search. You can also upload them from the chat.
        </p>
        <UploadButton documents={documents} />
      </div>
      <RoleNote refusal={refusal} />
      <DocumentsNotices
        storeStatus={storeStatus}
        storeError={storeError}
        documents={documents}
        className="px-3 py-2"
      />
      {filter.sources.length > 0 && (
        <select
          aria-label="Source"
          value={filter.chosen ?? ""}
          onChange={(e) => filter.choose(e.target.value ? Number(e.target.value) : undefined)}
          className={cn(selectClass, "w-auto self-start")}
        >
          <option value="">All sources</option>
          {filter.sources.map((source) => (
            <option key={source.id} value={source.id}>
              {source.name}
            </option>
          ))}
        </select>
      )}
      {documents.loading && <DocumentsLoading />}
      {isEmpty(documents) && (
        <p className="text-muted-foreground text-sm">
          No documents yet. Upload PDFs, Office files, web pages, text or images to search them.
        </p>
      )}
      {docs && docs.length === 0 && filter.chosen !== undefined && (
        <p className="text-muted-foreground text-sm">No documents from this source.</p>
      )}
      {docs && docs.length > 0 && (
        <ul className="divide-y rounded-md border" aria-label="Documents">
          {docs.map((doc) => (
            <li key={doc.id} className="group flex items-start gap-3 px-3 py-2">
              <FileText className="text-muted-foreground mt-0.5 size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm">{doc.filename}</p>
                <p className="text-muted-foreground text-xs">
                  <span title="Source">{doc.source.name}</span>
                  {" · "}
                  Uploaded {formatDate(doc.created_at)}
                  {doc.uploaded_by && ` by ${personName(doc.uploaded_by)}`}
                  {doc.status === "ready" &&
                    ` · ${doc.chunk_count} ${doc.chunk_count === 1 ? "chunk" : "chunks"}`}
                </p>
                {doc.status === "failed" && doc.error && (
                  <p className="text-destructive mt-0.5 text-xs break-words">{doc.error}</p>
                )}
                {doc.status === "ready" && doc.verdict !== "clean" && (
                  <p
                    className={cn(
                      "mt-0.5 text-xs break-words",
                      doc.verdict === "flagged" ? "text-destructive" : "text-muted-foreground",
                    )}
                  >
                    {verdictView(doc.verdict, doc.verdict_reason).detail}
                  </p>
                )}
              </div>
              <span className="text-muted-foreground w-16 shrink-0 text-right text-xs tabular-nums">
                {formatBytes(doc.size_bytes)}
              </span>
              <Badge variant={statusVariant[doc.status]}>
                {(doc.status === "pending" || doc.status === "processing") && (
                  <Loader2 className="animate-spin" />
                )}
                {statusLabel[doc.status]}
              </Badge>
              {doc.status === "ready" && <VerdictBadge doc={doc} />}
              {/* The API redirects to a link to the file that lasts a minute, minted per click,
                  which saves it under its own name; the page never holds one. A download, not a
                  navigation, so a refusal leaves the page (and any upload in flight) be. */}
              <a
                href={`${organisationApi()}/documents/${doc.id}/download`}
                download
                className="text-muted-foreground hover:text-foreground focus:opacity-100 md:opacity-0 md:group-hover:opacity-100"
                aria-label={`Download ${doc.filename}`}
              >
                <Download className="size-4" />
              </a>
              {!refusal && (
                <button
                  className="text-muted-foreground hover:text-destructive focus:opacity-100 md:opacity-0 md:group-hover:opacity-100"
                  onClick={() => onDelete(doc)}
                  aria-label={`Delete ${doc.filename}`}
                >
                  <Trash2 className="size-4" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
