import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, Loader2, Trash2, Upload, X } from "lucide-react";

import { formatDate } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError, describeError, type DataStoreStatus, type DocumentInfo } from "@/lib/api";
import { dataStoreNotice, dataStorePollInterval } from "@/lib/data-store";

const statusVariant = {
  pending: "secondary",
  processing: "secondary",
  ready: "outline",
  failed: "destructive",
} as const;

/**
 * The user's data store's status, asked again while it isn't ready. Unknown (undefined) until
 * the first answer; a failed request keeps the last one and tries again.
 */
export function useDataStoreStatus(): DataStoreStatus | undefined {
  const [status, setStatus] = useState<DataStoreStatus>();
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () =>
      api
        .getDataStore()
        .then(({ status }) => {
          if (stopped) return;
          setStatus(status);
          const wait = dataStorePollInterval(status);
          if (wait !== null) timer = setTimeout(check, wait);
        })
        .catch(() => {
          if (!stopped) timer = setTimeout(check, 5000);
        });
    check();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, []);
  return status;
}

/**
 * The user's documents, listed once their store is ready and re-read while any is still being
 * ingested, plus upload and delete. The chat's panel and the Data page's Documents both use it,
 * so they show the same list.
 */
export function useDocuments(storeStatus: DataStoreStatus | undefined) {
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  // What to say about the last read of the list failing (e.g. the store is busy); the list
  // keeps its last rows meanwhile.
  const [listError, setListError] = useState<string>();
  const [uploading, setUploading] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const ready = storeStatus === "ready";

  // Never rejects: a failed read keeps the last list, and the poll below tries again.
  const refresh = useCallback(
    () =>
      api.listDocuments().then(
        (list) => {
          setDocs(list);
          setListError(undefined);
        },
        (err) =>
          setListError(
            // 503: every connection the store allows is in use, which a moment's wait frees.
            err instanceof ApiError && err.status === 503
              ? "Your data store is busy. Trying again…"
              : `Couldn't load your documents (${describeError(err)}). Trying again…`,
          ),
      ),
    [],
  );

  // Until the store is ready there's nothing to list (the API answers 409).
  useEffect(() => {
    if (ready) refresh();
  }, [ready, refresh]);

  // Poll while anything is still being ingested by the worker, or the last read failed.
  const ingesting = docs.some((d) => d.status === "pending" || d.status === "processing");
  const retry = ready && Boolean(listError);
  useEffect(() => {
    if (!ingesting && !retry) return;
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [ingesting, retry, refresh]);

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
          failed.push(`${file.name}: ${describeError(err)}`);
        }
      }
    } finally {
      setErrors(failed);
      setUploading(false);
      await refresh();
    }
  }

  async function remove(doc: DocumentInfo) {
    setErrors([]);
    try {
      await api.deleteDocument(doc.id);
    } catch (err) {
      setErrors([`Couldn't delete ${doc.filename}: ${describeError(err)}`]);
    } finally {
      await refresh();
    }
  }

  return {
    docs,
    listError,
    uploading,
    errors,
    dismissErrors: () => setErrors([]),
    upload,
    remove,
    /** Uploads wait for the store to be ready, and for its status to be known. */
    canUpload: ready,
  };
}

type DocumentsState = ReturnType<typeof useDocuments>;

/** The Upload button, with the file picker it opens. */
export function UploadButton({ documents }: { documents: DocumentsState }) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <Button
        size="sm"
        variant="outline"
        disabled={!documents.canUpload}
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

/** What's wrong, if anything: a store that isn't ready, a list that couldn't be read, failed uploads. */
export function DocumentsNotices({
  storeStatus,
  documents,
}: {
  storeStatus: DataStoreStatus | undefined;
  documents: DocumentsState;
}) {
  const notice = storeStatus && dataStoreNotice(storeStatus);
  return (
    <>
      {notice && (
        <div
          role="status"
          aria-label="Data store"
          className="text-muted-foreground flex items-start gap-2 rounded-md border px-2 py-1.5 text-sm"
        >
          {storeStatus === "provisioning" && (
            <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" />
          )}
          <span>{notice}</span>
        </div>
      )}
      {documents.listError && (
        <div
          role="status"
          aria-label="Documents list"
          className="text-muted-foreground flex items-start gap-2 rounded-md border px-2 py-1.5 text-sm"
        >
          <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" />
          <span>{documents.listError}</span>
        </div>
      )}
      {documents.errors.length > 0 && (
        <div
          role="alert"
          className="border-destructive/50 text-destructive flex items-start gap-2 rounded-md border px-2 py-1.5 text-sm"
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

/** The chat's documents panel: a compact list beside the conversation it grounds. */
export function Documents() {
  const storeStatus = useDataStoreStatus();
  const documents = useDocuments(storeStatus);
  const { docs } = documents;

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Documents</h2>
        <UploadButton documents={documents} />
      </div>
      <DocumentsNotices storeStatus={storeStatus} documents={documents} />
      <ul className="flex-1 space-y-1 overflow-y-auto">
        {docs.length === 0 && documents.canUpload && (
          <li className="text-muted-foreground text-sm">Upload files to search them in chat.</li>
        )}
        {docs.map((doc) => (
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
            <button
              className="text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100"
              onClick={() => documents.remove(doc)}
              aria-label={`Delete ${doc.filename}`}
            >
              <Trash2 className="size-4" />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The Data page's Documents: every document with its ingest status (and why one failed), chunk
 * count and upload time. Uploads from the chat's panel land in the same list.
 */
export function DocumentsSection({ storeStatus }: { storeStatus: DataStoreStatus | undefined }) {
  const documents = useDocuments(storeStatus);
  const { docs } = documents;

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
      <DocumentsNotices storeStatus={storeStatus} documents={documents} />
      {docs.length === 0 && documents.canUpload && !documents.listError && (
        <p className="text-muted-foreground text-sm">
          No documents yet. Upload PDFs, Office files, web pages, text or images to search them.
        </p>
      )}
      {docs.length > 0 && (
        <ul className="divide-y rounded-md border" aria-label="Documents">
          {docs.map((doc) => (
            <li key={doc.id} className="group flex items-start gap-3 px-3 py-2">
              <FileText className="text-muted-foreground mt-0.5 size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm">{doc.filename}</p>
                <p className="text-muted-foreground text-xs">
                  Uploaded {formatDate(doc.created_at)}
                  {doc.status === "ready" &&
                    ` · ${doc.chunk_count} ${doc.chunk_count === 1 ? "chunk" : "chunks"}`}
                </p>
                {doc.status === "failed" && doc.error && (
                  <p className="text-destructive mt-0.5 text-xs break-words">{doc.error}</p>
                )}
              </div>
              <Badge variant={statusVariant[doc.status]}>
                {(doc.status === "pending" || doc.status === "processing") && (
                  <Loader2 className="animate-spin" />
                )}
                {statusLabel[doc.status]}
              </Badge>
              <button
                className="text-muted-foreground hover:text-destructive focus:opacity-100 md:opacity-0 md:group-hover:opacity-100"
                onClick={() => onDelete(doc)}
                aria-label={`Delete ${doc.filename}`}
              >
                <Trash2 className="size-4" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
