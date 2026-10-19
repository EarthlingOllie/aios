import { useState } from "react";
import { Loader2, Trash2 } from "lucide-react";

import { ErrorBanner, formatDate, Section, useLoad, usePoll } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { api, describeError } from "@/lib/api";
import {
  historyEmbeddingStatus,
  historyExportPending,
  historyExportStatus,
} from "@/lib/settings";

/**
 * The Organisation page's run history export: its switch, embedding, when it last ran, and
 * deleting what it copied. The organisation's, kept on its data store: any member changes it.
 */
export function RunHistoryExportSection() {
  const { data: settings, error: loadError, reload } = useLoad(api.getSettings);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  // What the switch was just set to, shown until the saved setting is read back.
  const [switching, setSwitching] = useState<boolean>();
  const [embedSwitching, setEmbedSwitching] = useState<boolean>();
  // Ask again while the store is being set up, or until the first export has run.
  usePoll(settings !== undefined && historyExportPending(settings), reload);

  async function act(fn: () => Promise<unknown>, done?: string) {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await fn();
      if (done) setNotice(done);
    } catch (err) {
      setError(describeError(err));
    } finally {
      await reload();
      setBusy(false);
    }
  }

  function onDelete() {
    // While the export is on, deleting starts it over: every run is exported again.
    const on = settings?.export_run_history;
    const again = settings?.embed_run_history ? "exported and embedded again" : "exported again";
    const question = on
      ? "Delete the run history exported to the data store? The runs stay here. The export is on, " +
        `so all the organisation's runs are ${again}: to keep it empty, switch the export off first.`
      : "Delete the run history exported to the data store? The runs stay here.";
    if (!window.confirm(question)) {
      return;
    }
    act(
      () => api.deleteExportedHistory(),
      on
        ? "Exported history deleted. All the organisation's runs are exported again."
        : "Exported history deleted.",
    );
  }

  return (
    <Section title="Run history export">
      <ErrorBanner message={loadError} />
      <p className="text-muted-foreground text-sm">
        Copy the organisation's agent runs (each conversation, with its messages, tool calls
        and usage) and workflow runs (with every step, at every depth), every member's and
        every schedule's, into its data store, where its agents can query them in its{" "}
        <code>history</code> tables, and search them with the Run history search tool. The runs
        here stay as they are: switching the export off keeps what was copied, and deleting a
        run deletes its copy while the export is on. Any member can change this.
      </p>
      {settings === undefined ? (
        !loadError && <Loader2 className="text-muted-foreground size-4 animate-spin" />
      ) : (
        <div className="flex flex-col gap-3 rounded-md border p-3">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={switching ?? settings.export_run_history}
              disabled={busy}
              onChange={(e) => {
                const on = e.target.checked;
                setSwitching(on);
                act(() => api.updateSettings({ export_run_history: on })).finally(() =>
                  setSwitching(undefined),
                );
              }}
            />
            Export the organisation's run history to its data store
          </label>
          <p
            role="status"
            aria-label="Run history export"
            className="text-muted-foreground text-sm"
          >
            {historyExportStatus(settings, formatDate)}
          </p>
          {/* Embedding is offered only while the export is on. */}
          {settings.export_run_history && (
            <div className="flex flex-col gap-1 border-t pt-3">
              <label className="flex items-center gap-2 text-sm font-medium">
                <input
                  type="checkbox"
                  checked={embedSwitching ?? settings.embed_run_history}
                  disabled={busy}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setEmbedSwitching(on);
                    act(() => api.updateSettings({ embed_run_history: on })).finally(() =>
                      setEmbedSwitching(undefined),
                    );
                  }}
                />
                Embed exported messages so agents can search past runs by meaning
              </label>
              <p
                role="status"
                aria-label="Run history embedding"
                className="text-muted-foreground text-sm"
              >
                {historyEmbeddingStatus(settings, formatDate)}
              </p>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="outline"
              size="sm"
              disabled={busy || !settings.history_ready}
              onClick={onDelete}
            >
              <Trash2 />
              Delete exported history
            </Button>
            {notice && <span className="text-muted-foreground text-sm">{notice}</span>}
          </div>
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        </div>
      )}
    </Section>
  );
}
