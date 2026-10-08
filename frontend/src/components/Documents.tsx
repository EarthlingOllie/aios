import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, Loader2, Trash2, Upload, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, describeError, type DocumentInfo } from "@/lib/api";

const statusVariant = {
  pending: "secondary",
  processing: "secondary",
  ready: "outline",
  failed: "destructive",
} as const;

export function Documents() {
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  const [uploading, setUploading] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  // Never rejects: a failed list (API restart, expired session) keeps the last list and the
  // poll below simply tries again.
  const refresh = useCallback(
    () =>
      api
        .listDocuments()
        .then(setDocs)
        .catch(() => {}),
    [],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll while anything is still being ingested by the worker.
  const busy = docs.some((d) => d.status === "pending" || d.status === "processing");
  useEffect(() => {
    if (!busy) return;
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [busy, refresh]);

  async function onFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setErrors([]);
    const failed: string[] = [];
    try {
      for (const file of Array.from(files)) {
        try {
          await api.uploadDocument(file);
        } catch (err) {
          failed.push(`${file.name}: ${describeError(err)}`);
        }
      }
    } finally {
      setErrors(failed);
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
      await refresh();
    }
  }

  async function onDelete(doc: DocumentInfo) {
    setErrors([]);
    try {
      await api.deleteDocument(doc.id);
    } catch (err) {
      setErrors([`Couldn't delete ${doc.filename}: ${describeError(err)}`]);
    } finally {
      await refresh();
    }
  }

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Documents</h2>
        <Button size="sm" variant="outline" onClick={() => inputRef.current?.click()}>
          {uploading ? <Loader2 className="animate-spin" /> : <Upload />}
          Upload
        </Button>
        <input
          ref={inputRef}
          type="file"
          multiple
          hidden
          accept=".pdf,.docx,.pptx,.xlsx,.html,.md,.txt,.csv,.png,.jpg,.jpeg"
          onChange={(e) => onFiles(e.target.files)}
        />
      </div>
      {errors.length > 0 && (
        <div
          role="alert"
          className="border-destructive/50 text-destructive flex items-start gap-2 rounded-md border px-2 py-1.5 text-sm"
        >
          <ul className="min-w-0 flex-1 space-y-0.5">
            {errors.map((msg, i) => (
              <li key={i} className="break-words">
                {msg}
              </li>
            ))}
          </ul>
          <button
            className="hover:text-foreground shrink-0"
            onClick={() => setErrors([])}
            aria-label="Dismiss errors"
          >
            <X className="size-4" />
          </button>
        </div>
      )}
      <ul className="flex-1 space-y-1 overflow-y-auto">
        {docs.length === 0 && (
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
              onClick={() => onDelete(doc)}
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
