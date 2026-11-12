import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import type { UIMessage } from "ai";
import { ArrowLeft, Brain, Trash2, Wrench } from "lucide-react";

import { AgentRunStatusBadge, RunCountersLine } from "@/components/agent-runs";
import { RunStatusBadge } from "@/components/workflow/status";
import {
  ErrorBanner,
  formatDate,
  Placeholder,
  useListHref,
  useLoad,
  usePoll,
  type ListContext,
} from "@/components/crud";
import { MarkdownView } from "@/components/Markdown";
import { Notice } from "@/components/Notice";
import { StartedBy } from "@/components/provenance";
import { SchemaChangeCard } from "@/components/Approvals";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api,
  describeError,
  type ContentBlock,
  type ModelMessage,
  type Run,
  type SchemaChangeRequest,
  type Turn,
} from "@/lib/api";

import { turnCounters } from "@/lib/agent-runs";
import { useRefusal } from "@/lib/current-user";
import { cn } from "@/lib/utils";

type Tab = "conversation" | "turns" | "events";

export function RunPage() {
  const agentId = Number(useParams().agentId);
  const runId = Number(useParams().runId);
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const run = useLoad(useCallback(() => api.getRun(agentId, runId), [agentId, runId]));
  const [tab, setTab] = useState<Tab>("conversation");
  const [deleteError, setDeleteError] = useState<string>();
  const deleteRefusal = useRefusal("build");
  // While a turn of the run is going on the run service, or the run waits on a schema change
  // request, read it again: to see the turn's reply, the request decided, and the rest of the
  // turn once the run carries on.
  usePoll(!!run.data?.waiting || run.data?.status === "running", run.reload, 2000);

  if (run.error) return <Placeholder>{run.error}</Placeholder>;
  if (!run.data) return null;

  async function onDelete() {
    if (!window.confirm("Delete this run and its history?")) return;
    try {
      await api.deleteRun(agentId, runId);
      reloadList();
      navigate(listHref(`/agents/${agentId}/runs`));
    } catch (err) {
      setDeleteError(describeError(err));
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <Button asChild variant="ghost" size="icon" aria-label="Back to agent">
          <Link to={listHref(`/agents/${agentId}/runs`)}>
            <ArrowLeft />
          </Link>
        </Button>
        <div className="min-w-0 flex-1">
          <div className="text-muted-foreground truncate text-xs">
            {run.data.agent_name} ·{" "}
            <Link to={listHref(`/agents/${agentId}/versions/${run.data.agent_version}`)} className="hover:underline">
              v{run.data.agent_version}
            </Link>{" "}
            · run #{runId} · started {formatDate(run.data.created_at)}
            <StartedBy run={run.data} before=" · " className="align-bottom" />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium">{run.data.title ?? "New run"}</span>
            <AgentRunStatusBadge run={run.data} data-testid="run-status" />
            <RunCountersLine
              run={run.data}
              className="text-muted-foreground text-xs"
              data-testid="run-counters"
            />
          </div>
        </div>
        <div className="bg-muted flex rounded-md p-0.5 text-sm" role="tablist">
          {(["conversation", "turns", "events"] as const).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={cn(
                "rounded px-3 py-1 capitalize",
                tab === t ? "bg-background shadow-xs" : "text-muted-foreground",
              )}
            >
              {t === "events" ? "Event log" : t}
            </button>
          ))}
        </div>
        {!deleteRefusal && (
          <Button variant="ghost" size="icon" onClick={onDelete} aria-label="Delete run">
            <Trash2 />
          </Button>
        )}
      </header>
      {deleteError && (
        <div className="px-4 pt-2">
          <ErrorBanner message={deleteError} onDismiss={() => setDeleteError(undefined)} />
        </div>
      )}
      {tab === "conversation" && (
        <div className="min-h-0 flex-1">
          <RunConversation run={run.data} onDecided={() => run.reload()} />
        </div>
      )}
      {tab === "turns" && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <TurnList turns={run.data.turns} />
        </div>
      )}
      {tab === "events" && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <EventLog messages={run.data.messages} instructions={run.data.instructions} />
        </div>
      )}
    </div>
  );
}

/** The run's conversation as it is stored, read-only: the run service runs its turns (for a
 * mention in a chat, a schedule or a workflow), and this shows what they said. A schema change
 * request its turn is waiting on can be decided from its card. */
function RunConversation({ run, onDecided }: { run: Run; onDecided: () => void }) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const messages = run.ui_messages;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <div role="log" aria-label="Conversation" className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
          {messages.length === 0 && (
            <p className="text-muted-foreground mt-24 text-center">Nothing has happened in this run yet.</p>
          )}
          {messages.map((message) => (
            <MessageView
              key={message.id}
              message={message}
              requests={run.schema_change_requests}
              onDecided={onDecided}
            />
          ))}
          <div ref={bottomRef} />
        </div>
      </div>
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-2 px-4 pb-3">
        {run.waiting && (
          <Notice label="Waiting" busy className="px-3 py-2">
            {run.schema_change_requests.some((r) => r.status === "pending")
              ? "The agent is waiting for you to approve or reject its schema change."
              : "The agent will carry on in a moment."}
          </Notice>
        )}
        <p role="note" aria-label="Read-only" className="text-muted-foreground text-center text-xs">
          This run is read-only. To talk to {run.agent_name}, mention {run.agent_name} in a chat.
        </p>
      </div>
    </div>
  );
}

function MessageView({
  message,
  requests,
  onDecided,
}: {
  message: UIMessage;
  requests: SchemaChangeRequest[];
  onDecided: () => void;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-2",
        message.role === "user" && "bg-secondary max-w-[80%] self-end rounded-2xl px-4 py-2",
      )}
    >
      {message.parts.map((part, i) => {
        if (part.type === "text") {
          return <MarkdownView key={i}>{part.text}</MarkdownView>;
        }
        if (part.type === "reasoning") {
          return (
            <details key={i} className="text-muted-foreground text-xs">
              <summary className="flex cursor-pointer items-center gap-1.5">
                <Brain className="size-3" />
                Thinking
              </summary>
              <p className="mt-1 border-l pl-3 whitespace-pre-wrap">{part.text}</p>
            </details>
          );
        }
        if (part.type === "dynamic-tool" || part.type.startsWith("tool-")) {
          const tool = part as {
            type: string;
            toolCallId?: string;
            toolName?: string;
            state?: string;
            input?: unknown;
            output?: unknown;
            errorText?: string;
          };
          const request = requests.find((r) => r.tool_call_id && r.tool_call_id === tool.toolCallId);
          if (request) {
            return <SchemaChangeCard key={i} request={request} onDecided={onDecided} />;
          }
          return (
            <ToolCall
              key={i}
              name={tool.toolName ?? tool.type.slice("tool-".length)}
              state={tool.state}
              input={tool.input}
              output={tool.errorText ?? tool.output}
              failed={tool.state === "output-error"}
            />
          );
        }
        return null;
      })}
    </div>
  );
}

function ToolCall({
  name,
  state,
  input,
  output,
  failed,
}: {
  name: string;
  state?: string;
  input: unknown;
  output: unknown;
  failed?: boolean;
}) {
  return (
    <details className="rounded-md border text-xs">
      <summary className="flex cursor-pointer items-center gap-2 px-3 py-1.5">
        <Wrench className="text-muted-foreground size-3" />
        <span className="font-mono">{name}</span>
        {state && (
          <Badge variant={failed ? "destructive" : "secondary"} className="ml-auto">
            {state}
          </Badge>
        )}
      </summary>
      <div className="flex flex-col gap-2 border-t px-3 py-2">
        {input !== undefined && <JsonBlock label="Input" value={input} />}
        {output !== undefined && <JsonBlock label={failed ? "Error" : "Output"} value={output} />}
      </div>
    </details>
  );
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <div className="text-muted-foreground mb-1 font-medium">{label}</div>
      <pre className="bg-muted/50 max-h-64 overflow-auto rounded p-2 whitespace-pre-wrap">
        {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

// --- Event log: the stored LangGraph history, message by message ------------------------------

const KIND_LABEL: Record<ModelMessage["type"], string> = {
  human: "User",
  ai: "Model",
  tool: "Tool result",
  system: "System",
};

function blocks(message: ModelMessage): ContentBlock[] {
  if (typeof message.content === "string") {
    return message.content ? [{ type: "text", text: message.content }] : [];
  }
  return message.content.map((b) => (typeof b === "string" ? { type: "text", text: b } : b));
}

/** The run's turns, oldest first: each with the version it ran on, how it went, how long it
 * took and what it cost. */
function TurnList({ turns }: { turns: Turn[] }) {
  if (turns.length === 0) return <Placeholder>No turns yet.</Placeholder>;
  return (
    <ol className="mx-auto flex max-w-3xl flex-col gap-2 px-4 py-6">
      {turns.map((turn, i) => (
        <li key={turn.id} className="rounded-md border px-3 py-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">Turn {i + 1}</span>
            <RunStatusBadge status={turn.status} />
            <span className="text-muted-foreground text-xs">
              v{turn.agent_version} · {formatDate(turn.started_at)}
            </span>
          </div>
          <div className="text-muted-foreground mt-1 text-xs tabular-nums">{turnCounters(turn)}</div>
          {turn.error && <p className="text-destructive mt-1 text-xs">{turn.error}</p>}
        </li>
      ))}
    </ol>
  );
}

function EventLog({ messages, instructions }: { messages: ModelMessage[]; instructions: string }) {
  if (messages.length === 0) return <Placeholder>Nothing has happened in this run yet.</Placeholder>;

  const responses = messages.filter((m) => m.type === "ai");
  // Provider-run tools (e.g. web search) are content blocks of the response, not tool calls.
  const toolCalls = responses.reduce(
    (n, m) =>
      n + (m.tool_calls?.length ?? 0) + blocks(m).filter((b) => b.type === "server_tool_use").length,
    0,
  );
  const tokens = (key: "input_tokens" | "output_tokens") =>
    responses.reduce((sum, m) => sum + (m.usage_metadata?.[key] ?? 0), 0);

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ["Model calls", responses.length],
          ["Tool calls", toolCalls],
          ["Input tokens", tokens("input_tokens")],
          ["Output tokens", tokens("output_tokens")],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md border px-3 py-2">
            <dt className="text-muted-foreground text-xs">{label}</dt>
            <dd className="text-lg font-semibold tabular-nums">{value.toLocaleString()}</dd>
          </div>
        ))}
      </dl>
      {instructions && (
        <details className="rounded-md border px-3 py-2 text-xs">
          <summary className="text-muted-foreground cursor-pointer">
            Instructions (sent with every model call)
          </summary>
          <pre className="bg-muted/50 mt-1 max-h-64 overflow-auto rounded p-2 whitespace-pre-wrap">
            {instructions}
          </pre>
        </details>
      )}
      <ol className="flex flex-col gap-3">
        {messages.map((message, i) => (
          <li key={message.id ?? i} className="rounded-md border">
            <div className="bg-muted/40 flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-xs">
              <Badge variant={message.type === "ai" ? "secondary" : "outline"}>
                {KIND_LABEL[message.type] ?? message.type}
              </Badge>
              {message.response_metadata?.model_name && (
                <span className="font-mono">{message.response_metadata.model_name}</span>
              )}
              {message.type === "ai" && message.usage_metadata && (
                <span className="text-muted-foreground">
                  {message.usage_metadata.input_tokens ?? 0} in /{" "}
                  {message.usage_metadata.output_tokens ?? 0} out
                </span>
              )}
              {message.response_metadata?.stop_reason && (
                <span className="text-muted-foreground ml-auto">
                  {message.response_metadata.stop_reason}
                </span>
              )}
            </div>
            <div className="flex flex-col gap-2 p-3">
              <MessageEvents message={message} />
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

function MessageEvents({ message }: { message: ModelMessage }) {
  if (message.type === "tool") {
    return (
      <ToolCall
        name={message.name ?? "tool"}
        state={message.status === "error" ? "failed" : "return"}
        failed={message.status === "error"}
        input={undefined}
        output={parseArgs(message.content)}
      />
    );
  }
  return (
    <>
      {blocks(message).map((block, j) => (
        <EventBlock key={j} block={block} />
      ))}
      {message.tool_calls?.map((call) => (
        <ToolCall key={call.id} name={call.name} state="call" input={call.args} output={undefined} />
      ))}
    </>
  );
}

function EventBlock({ block }: { block: ContentBlock }) {
  const b = block as ContentBlock & Record<string, unknown>;
  switch (b.type) {
    case "text":
    case "thinking":
      return (
        <div className="text-sm">
          <div className="text-muted-foreground mb-0.5 text-xs">{b.type}</div>
          <p className="whitespace-pre-wrap">{String(b.type === "text" ? b.text : b.thinking)}</p>
        </div>
      );
    // Shown from the message's tool calls instead.
    case "tool_use":
      return null;
    case "server_tool_use":
      return <ToolCall name={String(b.name)} state="call" input={b.input} output={undefined} />;
    default:
      if (b.type.endsWith("_tool_result")) {
        const failed = !!(b.content && typeof b.content === "object" && "error_code" in b.content);
        return (
          <ToolCall
            name={b.type.replace(/_tool_result$/, "")}
            state={failed ? "failed" : "return"}
            failed={failed}
            input={undefined}
            output={b.content}
          />
        );
      }
      return (
        <pre className="bg-muted/50 overflow-auto rounded p-2 text-xs">
          {JSON.stringify(b, null, 2)}
        </pre>
      );
  }
}

function parseArgs(args: unknown) {
  if (typeof args !== "string") return args;
  try {
    return JSON.parse(args);
  } catch {
    return args;
  }
}
