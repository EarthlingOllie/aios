import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, X } from "lucide-react";

import { ErrorBanner, Section, selectClass } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, describeError, type AgentSummary, type DefaultChatAgent, type Organisation } from "@/lib/api";
import { moved } from "@/lib/default-chat-agents";

/**
 * The Organisation page's default chat agents: the agents that join every new chat, in order
 * (but chats made by **Chat with this agent**), starting as the organisation assistant. Admins
 * add, remove and reorder them; everyone else sees the list. An archived agent stays on the
 * list, marked archived, and is skipped when a chat is made.
 */
export function DefaultChatAgentsSection({ org }: { org: Organisation }) {
  const admin = org.role === "admin";
  const [saved, setSaved] = useState<DefaultChatAgent[]>();
  const [draft, setDraft] = useState<DefaultChatAgent[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [adding, setAdding] = useState("");
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    let live = true;
    api
      .getDefaultChatAgents()
      .then((list) => {
        if (!live) return;
        setSaved(list);
        setDraft(list);
      })
      .catch((err) => live && setError(describeError(err)));
    if (admin) {
      api
        .listAgents()
        .then((list) => live && setAgents(list))
        .catch((err) => live && setError(describeError(err)));
    }
    return () => {
      live = false;
    };
  }, [admin]);

  const changed = saved !== undefined && draft.map((a) => a.id).join() !== saved.map((a) => a.id).join();
  const addable = agents.filter((a) => !draft.some((d) => d.id === a.id));

  function edit(next: DefaultChatAgent[]) {
    setDone(false);
    setDraft(next);
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      const list = await api.setDefaultChatAgents(draft.map((a) => a.id));
      setSaved(list);
      setDraft(list);
      setDone(true);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  const items = (
    <ol aria-label="Default chat agents" className="flex flex-col gap-1">
      {draft.map((agent, i) => (
        <li key={agent.id} className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground w-5 text-right tabular-nums">{i + 1}.</span>
          <span className="min-w-0 flex-1 truncate">{agent.name}</span>
          {agent.archived && (
            <Badge variant="outline" title="Archived agents are skipped when a chat is made">
              Archived
            </Badge>
          )}
          {admin && (
            <>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Move ${agent.name} up`}
                disabled={saving || i === 0}
                onClick={() => edit(moved(draft, i, i - 1))}
              >
                <ArrowUp />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Move ${agent.name} down`}
                disabled={saving || i === draft.length - 1}
                onClick={() => edit(moved(draft, i, i + 1))}
              >
                <ArrowDown />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Remove ${agent.name}`}
                disabled={saving}
                onClick={() => edit(draft.filter((a) => a.id !== agent.id))}
              >
                <X />
              </Button>
            </>
          )}
        </li>
      ))}
    </ol>
  );

  return (
    <Section title="Default chat agents" label="Default chat agents">
      <p className="text-muted-foreground text-sm">
        The agents that join every new chat, in this order. Chats made with &ldquo;Chat with this agent&rdquo; get only
        that agent, archived agents are skipped, and changes affect new chats only.
      </p>
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {saved === undefined ? null : admin ? (
        <form className="flex flex-col gap-3 rounded-md border p-3" onSubmit={onSubmit}>
          {draft.length === 0 ? <p className="text-muted-foreground text-sm">No agents join new chats.</p> : items}
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Add an agent"
              className={selectClass + " w-auto"}
              value={adding}
              disabled={saving || addable.length === 0}
              onChange={(e) => setAdding(e.target.value)}
            >
              <option value="">Add an agent…</option>
              {addable.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={saving || adding === ""}
              onClick={() => {
                const agent = agents.find((a) => a.id === Number(adding));
                if (agent) edit([...draft, { id: agent.id, name: agent.name, archived: agent.archived_at !== null }]);
                setAdding("");
              }}
            >
              Add
            </Button>
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={saving || !changed}>
              Apply
            </Button>
            {done && (
              <span role="status" className="text-muted-foreground text-sm">
                Saved
              </span>
            )}
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-2 rounded-md border p-3 text-sm">
          {draft.length === 0 ? <p>No agents join new chats.</p> : items}
          <p className="text-muted-foreground">Only admins can change which agents join new chats.</p>
        </div>
      )}
    </Section>
  );
}
