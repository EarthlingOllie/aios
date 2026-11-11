import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { ErrorBanner } from "@/components/crud";
import { Button } from "@/components/ui/button";
import {
  api,
  describeError,
  type Agent,
  type AgentUsedBy,
  type Skill,
  type SkillUsedBy,
  type UsedByTrigger,
  type UsedByWorkflow,
  type Workflow,
  type WorkflowUsedBy,
} from "@/lib/api";

function Group({ title, empty, children }: { title: string; empty?: boolean; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1">
      <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">{title}</h3>
      {empty ? <p className="text-muted-foreground text-sm">None</p> : children}
    </section>
  );
}

function schedule(t: UsedByTrigger): string {
  return `${t.description} (${t.timezone})${t.enabled ? "" : " · paused"}`;
}

function Workflows({ title, workflows }: { title: string; workflows: UsedByWorkflow[] }) {
  return (
    <Group title={title} empty={!workflows.length}>
      <ul className="flex flex-col gap-0.5 text-sm">
        {workflows.map((w) => (
          <li key={w.id}>
            <Link to={`/workflows/${w.id}`} className="hover:underline">
              {w.name}
            </Link>
            {w.direct ? "" : " (through a nested workflow)"}
          </li>
        ))}
      </ul>
    </Group>
  );
}

function Schedules({ triggers }: { triggers: UsedByTrigger[] }) {
  return (
    <Group title="Its schedules, which pause" empty={!triggers.length}>
      <ul className="flex flex-col gap-0.5 text-sm">
        {triggers.map((t) => (
          <li key={t.id}>{schedule(t)}</li>
        ))}
      </ul>
    </Group>
  );
}

function Chats({ chats }: { chats: { id: number; name: string }[] }) {
  return (
    <Group title="Chats it's in" empty={!chats.length}>
      <ul className="flex flex-col gap-0.5 text-sm">
        {chats.map((c) => (
          <li key={c.id}>
            <Link to={`/chats/${c.id}`} className="hover:underline">
              {c.name}
            </Link>
          </li>
        ))}
      </ul>
    </Group>
  );
}

/**
 * Archiving an agent, workflow or skill: one confirmation, never blocked. It shows what uses the
 * item (its `GET .../used-by`), read-only; archiving doesn't wait for the list, which is only there
 * so the member knows what will stop.
 */
function ArchiveDialog<T, U>({
  kind,
  name,
  about,
  loadUsedBy,
  usedBy: renderUsedBy,
  archive,
  onClose,
  onArchived,
}: {
  kind: "agent" | "workflow" | "skill";
  name: string;
  /** What archiving does. */
  about: string;
  loadUsedBy: () => Promise<U>;
  usedBy: (usedBy: U) => ReactNode;
  archive: () => Promise<T>;
  onClose: () => void;
  onArchived: (item: T) => void | Promise<void>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [usedBy, setUsedBy] = useState<U>();
  const [loadError, setLoadError] = useState<string>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  // Loaded once, as the dialog opens; the loader is a fresh closure on every render.
  const load = useRef(loadUsedBy);
  useEffect(() => {
    ref.current?.showModal();
    load.current().then(setUsedBy, (err) => setLoadError(describeError(err)));
  }, []);

  async function onArchive() {
    setBusy(true);
    setError(undefined);
    try {
      await onArchived(await archive());
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={ref}
      aria-labelledby={`${kind}-archive-title`}
      // Escape, or the Cancel button: either closes it.
      onClose={onClose}
      className="bg-background text-foreground m-auto w-[min(36rem,calc(100vw-2rem))] rounded-lg border p-0 shadow-lg backdrop:bg-black/40"
    >
      <div className="flex max-h-[80vh] flex-col">
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-5 pb-3">
          <h2 id={`${kind}-archive-title`} className="text-lg font-semibold">
            Archive “{name}”?
          </h2>
          <p className="text-sm">{about}</p>
          <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
          <ErrorBanner message={loadError} />
          {usedBy === undefined && !loadError && (
            <p className="text-muted-foreground text-sm">Checking what uses this {kind}…</p>
          )}
          {usedBy !== undefined && (
            <div aria-label="Used by" className="flex flex-col gap-3">
              {renderUsedBy(usedBy)}
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t px-5 py-3">
          <Button disabled={busy} onClick={onArchive}>
            Archive {kind}
          </Button>
          <Button variant="ghost" onClick={() => ref.current?.close()}>
            Cancel
          </Button>
        </div>
      </div>
    </dialog>
  );
}

type DialogProps<T> = {
  item: { id: number; name: string };
  onClose: () => void;
  onArchived: (item: T) => void | Promise<void>;
};

/** The workflows that run it, its schedules (which pause), its chats, and the data tables it
 * will leave read-only. */
export function AgentArchiveDialog({ item, onClose, onArchived }: DialogProps<Agent>) {
  return (
    <ArchiveDialog<Agent, AgentUsedBy>
      kind="agent"
      name={item.name}
      about="It starts nothing new: no runs by hand, by schedule, by mention or in a workflow. What's already going finishes, and nothing is lost. You can unarchive it at any time."
      loadUsedBy={() => api.getAgentUsedBy(item.id)}
      usedBy={(u) => (
        <>
          <Workflows title="Workflows that stop starting" workflows={u.workflows} />
          <Schedules triggers={u.triggers} />
          <Chats chats={u.chats} />
          <Group title="Data tables left read-only" empty={!u.tables.length}>
            <ul className="flex flex-col gap-0.5 text-sm">
              {u.tables.map((t) => (
                <li key={t.name}>{t.name}</li>
              ))}
            </ul>
          </Group>
        </>
      )}
      archive={() => api.archiveAgent(item.id)}
      onClose={onClose}
      onArchived={onArchived}
    />
  );
}

/** The workflows that nest it, its schedules (which pause) and its chats. */
export function WorkflowArchiveDialog({ item, onClose, onArchived }: DialogProps<Workflow>) {
  return (
    <ArchiveDialog<Workflow, WorkflowUsedBy>
      kind="workflow"
      name={item.name}
      about="It starts nothing new: no runs by hand, by schedule, by mention or nested in another workflow. Runs already going finish, and nothing is lost. You can unarchive it at any time."
      loadUsedBy={() => api.getWorkflowUsedBy(item.id)}
      usedBy={(u) => (
        <>
          <Workflows title="Workflows that stop starting" workflows={u.workflows} />
          <Schedules triggers={u.triggers} />
          <Chats chats={u.chats} />
        </>
      )}
      archive={() => api.archiveWorkflow(item.id)}
      onClose={onClose}
      onArchived={onArchived}
    />
  );
}

/** The agents whose current version uses it, which keep running with it. */
export function SkillArchiveDialog({ item, onClose, onArchived }: DialogProps<Skill>) {
  return (
    <ArchiveDialog<Skill, SkillUsedBy>
      kind="skill"
      name={item.name}
      about="It can't be added to an agent from now on. Agents that use it keep running with it, but a new version of one can't include it. You can unarchive it at any time."
      loadUsedBy={() => api.getSkillUsedBy(item.id)}
      usedBy={(u) => (
        <Group title="Agents that use it" empty={!u.agents.length}>
          <ul className="flex flex-col gap-0.5 text-sm">
            {u.agents.map((a) => (
              <li key={a.agent_id}>
                <Link to={`/agents/${a.agent_id}`} className="hover:underline">
                  {a.agent_name}
                </Link>
              </li>
            ))}
          </ul>
        </Group>
      )}
      archive={() => api.archiveSkill(item.id)}
      onClose={onClose}
      onArchived={onArchived}
    />
  );
}
