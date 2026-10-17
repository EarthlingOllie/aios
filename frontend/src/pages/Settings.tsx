import { useState } from "react";
import { Loader2, Trash2 } from "lucide-react";

import { ErrorBanner, formatDate, Section, useLoad, usePoll } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { api, describeError } from "@/lib/api";
import { historyExportPending, historyExportStatus } from "@/lib/settings";

/** The user's settings. So far, the run history export. */
export function SettingsPage() {
  const { data: settings, error: loadError, reload } = useLoad(api.getSettings);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  // What the switch was just set to, shown until the saved setting is read back.
  const [switching, setSwitching] = useState<boolean>();
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
    const question = on
      ? "Delete the run history exported to your data store? Your runs stay here. The export is " +
        "on, so all your runs are exported again: to keep it empty, switch the export off first."
      : "Delete the run history exported to your data store? Your runs stay here.";
    if (!window.confirm(question)) {
      return;
    }
    act(
      () => api.deleteExportedHistory(),
      on
        ? "Exported history deleted. All your runs are exported again."
        : "Exported history deleted.",
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
        <header>
          <h1 className="text-xl font-semibold">Settings</h1>
          <p className="text-muted-foreground mt-1 text-sm">Manage your account.</p>
        </header>
        <ErrorBanner message={loadError} />
        <Section title="Run history export">
          <p className="text-muted-foreground text-sm">
            Copy your agent runs (each conversation, with its messages, tool calls and usage) and
            workflow runs (with every step, at every depth) into your data store, where your
            agents can query them in its <code>history</code> tables.
            Your runs here stay as they are: switching the export off keeps what was copied, and
            deleting a run deletes its copy while the export is on.
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
                Export my run history to my data store
              </label>
              <p
                role="status"
                aria-label="Run history export"
                className="text-muted-foreground text-sm"
              >
                {historyExportStatus(settings, formatDate)}
              </p>
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
      </div>
    </div>
  );
}
