import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { ErrorBanner } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { api, describeError, type Agent, type AgentUsedBy, type DependentTrigger } from "@/lib/api";

function Group({ title, empty, children }: { title: string; empty?: boolean; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1">
      <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">{title}</h3>
      {empty ? <p className="text-muted-foreground text-sm">None</p> : children}
    </section>
  );
}

function schedule(t: DependentTrigger): string {
  return `${t.description} (${t.timezone})${t.enabled ? "" : " · paused"}`;
}

/**
 * Archiving an agent: one confirmation, never blocked. It shows what uses the agent (`GET
 * /api/agents/{id}/used-by`), read-only: the workflows that run it, its schedules (which pause),
 * the chats it's in, and the data tables it will leave read-only. Archiving doesn't wait for the
 * list: it's only there so the member knows what will stop.
 */
export function AgentArchiveDialog({
  agent,
  onClose,
  onArchived,
}: {
  agent: { id: number; name: string };
  onClose: () => void;
  onArchived: (agent: Agent) => void | Promise<void>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [usedBy, setUsedBy] = useState<AgentUsedBy>();
  const [loadError, setLoadError] = useState<string>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    ref.current?.showModal();
    api.getAgentUsedBy(agent.id).then(setUsedBy, (err) => setLoadError(describeError(err)));
  }, [agent.id]);

  async function onArchive() {
    setBusy(true);
    setError(undefined);
    try {
      await onArchived(await api.archiveAgent(agent.id));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={ref}
      aria-labelledby="agent-archive-title"
      // Escape, or the Cancel button: either closes it.
      onClose={onClose}
      className="bg-background text-foreground m-auto w-[min(36rem,calc(100vw-2rem))] rounded-lg border p-0 shadow-lg backdrop:bg-black/40"
    >
      <div className="flex max-h-[80vh] flex-col">
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-5 pb-3">
          <h2 id="agent-archive-title" className="text-lg font-semibold">
            Archive “{agent.name}”?
          </h2>
          <p className="text-sm">
            It starts nothing new: no runs by hand, by schedule, by mention or in a workflow. What's
            already going finishes, and nothing is lost. You can unarchive it at any time.
          </p>
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
          <ErrorBanner message={loadError} />
          {!usedBy && !loadError && (
            <p className="text-muted-foreground text-sm">Checking what uses this agent…</p>
          )}
          {usedBy && (
            <div aria-label="Used by" className="flex flex-col gap-3">
              <Group title="Workflows that stop starting" empty={!usedBy.workflows.length}>
                <ul className="flex flex-col gap-0.5 text-sm">
                  {usedBy.workflows.map((w) => (
                    <li key={w.id}>
                      <Link to={`/workflows/${w.id}`} className="hover:underline">
                        {w.name}
                      </Link>
                      {w.direct ? "" : " (through a nested workflow)"}
                    </li>
                  ))}
                </ul>
              </Group>
              <Group title="Its schedules, which pause" empty={!usedBy.triggers.length}>
                <ul className="flex flex-col gap-0.5 text-sm">
                  {usedBy.triggers.map((t) => (
                    <li key={t.id}>{schedule(t)}</li>
                  ))}
                </ul>
              </Group>
              <Group title="Chats it's in" empty={!usedBy.chats.length}>
                <ul className="flex flex-col gap-0.5 text-sm">
                  {usedBy.chats.map((c) => (
                    <li key={c.id}>
                      <Link to={`/chats/${c.id}`} className="hover:underline">
                        {c.name}
                      </Link>
                    </li>
                  ))}
                </ul>
              </Group>
              <Group title="Data tables left read-only" empty={!usedBy.tables.length}>
                <ul className="flex flex-col gap-0.5 text-sm">
                  {usedBy.tables.map((t) => (
                    <li key={t.name}>{t.name}</li>
                  ))}
                </ul>
              </Group>
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t px-5 py-3">
          <Button disabled={busy} onClick={onArchive}>
            Archive agent
          </Button>
          <Button variant="ghost" onClick={() => ref.current?.close()}>
            Cancel
          </Button>
        </div>
      </div>
    </dialog>
  );
}
