import { useState } from "react";
import { Link } from "react-router";
import { Pencil, Trash2 } from "lucide-react";

import { ErrorBanner, formatDate } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api, describeError, type AgentMemory } from "@/lib/api";
import { personName } from "@/lib/provenance";

/** Where a memory was learned, linked to its chat or run while they're still there. */
function LearnedIn({ memory, runHref }: { memory: AgentMemory; runHref: (id: number) => string }) {
  if (memory.chat_id !== null) {
    return (
      <>
        Learned in the chat{" "}
        <Link to={`/chats/${memory.chat_id}`} className="hover:underline">
          {memory.chat_name}
        </Link>
      </>
    );
  }
  if (memory.agent_run_id !== null) {
    return (
      <>
        Learned in the run{" "}
        <Link to={runHref(memory.agent_run_id)} className="hover:underline">
          {memory.run_title ?? "Untitled run"}
        </Link>
      </>
    );
  }
  return <>Learned in a run that's no longer kept</>;
}

function MemoryRow({
  memory,
  canEdit,
  runHref,
  onSave,
  onDelete,
}: {
  memory: AgentMemory;
  canEdit: boolean;
  runHref: (id: number) => string;
  onSave: (content: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    try {
      await fn();
      setDraft(undefined);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex flex-col gap-2 px-3 py-2" data-testid="memory">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {draft === undefined ? (
        <div className="flex items-start gap-2">
          <p className="min-w-0 flex-1 text-sm whitespace-pre-wrap">{memory.content}</p>
          {canEdit && (
            <>
              <Button
                size="sm"
                variant="ghost"
                aria-label="Edit memory"
                disabled={busy}
                onClick={() => setDraft(memory.content)}
              >
                <Pencil />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                aria-label="Delete memory"
                disabled={busy}
                onClick={() => {
                  if (window.confirm("Delete this memory? The agent won't recall it again.")) {
                    act(onDelete);
                  }
                }}
              >
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      ) : (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            act(() => onSave(draft));
          }}
        >
          <Textarea
            aria-label="Memory"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
          />
          <div className="flex gap-2">
            <Button size="sm" type="submit" disabled={busy || !draft.trim()}>
              Save
            </Button>
            <Button size="sm" variant="outline" type="button" onClick={() => setDraft(undefined)}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      <p className="text-muted-foreground text-xs">
        <LearnedIn memory={memory} runHref={runHref} /> · {formatDate(memory.created_at)}
        {memory.updated_by && ` · Edited by ${personName(memory.updated_by)}`}
      </p>
    </li>
  );
}

/**
 * The agent's Memory tab: what it chose to remember (its `remember` tool), with where each was
 * learned. Editors and admins correct or delete them (`canEdit`); everyone else only reads.
 */
export function AgentMemories({
  agentId,
  memories,
  error,
  canEdit,
  runHref,
  reload,
}: {
  agentId: number;
  memories: AgentMemory[] | undefined;
  error: string | undefined;
  canEdit: boolean;
  runHref: (id: number) => string;
  reload: () => Promise<unknown>;
}) {
  if (error) return <p className="text-destructive text-sm">{error}</p>;
  if (!memories) return null;
  if (memories.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        No memories yet. An agent with the Remember tool keeps facts here for later runs.
      </p>
    );
  }
  return (
    <ul className="divide-y rounded-md border">
      {memories.map((memory) => (
        <MemoryRow
          key={memory.id}
          memory={memory}
          canEdit={canEdit}
          runHref={runHref}
          onSave={async (content) => {
            await api.editMemory(agentId, memory.id, content);
            await reload();
          }}
          onDelete={async () => {
            await api.deleteMemory(agentId, memory.id);
            await reload();
          }}
        />
      ))}
    </ul>
  );
}
