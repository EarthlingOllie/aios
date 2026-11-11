import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Download, Upload } from "lucide-react";

import { ErrorBanner, useListHref } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { api, describeError, type ImportResult } from "@/lib/api";
import { importedPath, importLines } from "@/lib/imports";
import { useRefusal } from "@/lib/current-user";

/** An exported file's import: choosing it previews what it makes (a dry run), and Import makes
 * it, then opens what was made. Nothing is made until Import. */
function ImportDialog({
  onClose,
  onImported,
}: {
  onClose: () => void;
  onImported: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const navigate = useNavigate();
  const listHref = useListHref();
  const [file, setFile] = useState<File>();
  const [preview, setPreview] = useState<ImportResult>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  async function onChoose(chosen: File | undefined) {
    setFile(chosen);
    setPreview(undefined);
    setError(undefined);
    if (!chosen) return;
    setBusy(true);
    try {
      setPreview(await api.importFile(chosen, true));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function onImport() {
    if (!file) return;
    setBusy(true);
    setError(undefined);
    try {
      const result = await api.importFile(file, false);
      onImported();
      ref.current?.close();
      const path = importedPath(result);
      if (path) navigate(listHref(path));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={ref}
      aria-labelledby="import-title"
      onClose={onClose}
      className="bg-background text-foreground m-auto w-[min(36rem,calc(100vw-2rem))] rounded-lg border p-0 shadow-lg backdrop:bg-black/40"
    >
      <div className="flex max-h-[80vh] flex-col">
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-5 pb-3">
          <h2 id="import-title" className="text-lg font-semibold">
            Import an agent or workflow
          </h2>
          <p className="text-sm">
            Choose a file exported from an organisation. It's imported as new
            copies at version 1, unconnected to the originals; skills are always
            copied, and a name already taken gets “(imported)”.
          </p>
          <input
            type="file"
            accept="application/json,.json"
            aria-label="Exported file"
            onChange={(e) => onChoose(e.target.files?.[0])}
            className="text-sm"
          />
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
          {preview && (
            <section
              aria-label="Import preview"
              className="flex flex-col gap-1"
            >
              <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                This import makes
              </h3>
              <ul className="flex flex-col gap-1 text-sm">
                {importLines(preview.items).map((line) => (
                  <li key={line.key}>
                    {line.text}
                    {line.note && (
                      <span className="text-muted-foreground">
                        {" "}
                        · {line.note}
                      </span>
                    )}
                    {line.warning && (
                      <span
                        role="note"
                        className="block text-amber-700 dark:text-amber-400"
                      >
                        {line.warning}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t px-5 py-3">
          <Button disabled={busy || !preview} onClick={onImport}>
            Import
          </Button>
          <Button variant="ghost" onClick={() => ref.current?.close()}>
            Cancel
          </Button>
        </div>
      </div>
    </dialog>
  );
}

/** The Import button for a list's header: shown to those who may build (editors and admins). */
export function ImportButton({ onImported }: { onImported: () => void }) {
  const refusal = useRefusal("build");
  const [open, setOpen] = useState(false);
  if (refusal) return null;
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Upload />
        Import
      </Button>
      {open && (
        <ImportDialog onClose={() => setOpen(false)} onImported={onImported} />
      )}
    </>
  );
}

/** The Export link for an agent's or workflow's page: downloads its current version's file. */
export function ExportLink({ href }: { href: string }) {
  return (
    <Button asChild variant="outline" size="sm">
      <a href={href} download>
        <Download />
        Export
      </a>
    </Button>
  );
}

/** Says why the version can't run here (a tool it needs is missing, say), if it can't. */
export function UnrunnableNotice({
  reason,
}: {
  reason: string | null | undefined;
}) {
  if (!reason) return null;
  return (
    <p
      role="note"
      className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm dark:border-amber-700 dark:bg-amber-950"
    >
      {reason} Edit it to remove or replace what's missing, or set up what it
      needs.
    </p>
  );
}
