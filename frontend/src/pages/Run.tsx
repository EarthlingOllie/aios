import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type UIMessage } from "ai";
import { ArrowLeft, ArrowUp, Brain, Square, Trash2, Wrench } from "lucide-react";
import Markdown from "react-markdown";

import { ErrorBanner, formatDate, Placeholder, useLoad, type ListContext } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api, describeError, type ModelMessage, type Run } from "@/lib/api";
import { cn } from "@/lib/utils";

type Tab = "conversation" | "events";

export function RunPage() {
  const agentId = Number(useParams().agentId);
  const runId = Number(useParams().runId);
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const agent = useLoad(useCallback(() => api.getAgent(agentId), [agentId]));
  const run = useLoad(useCallback(() => api.getRun(agentId, runId), [agentId, runId]));
  const [tab, setTab] = useState<Tab>("conversation");
  const [deleteError, setDeleteError] = useState<string>();

  if (run.error) return <Placeholder>{run.error}</Placeholder>;
  if (!run.data) return null;

  async function onDelete() {
    if (!window.confirm("Delete this run and its history?")) return;
    try {
      await api.deleteRun(agentId, runId);
      reloadList();
      navigate(`/agents/${agentId}`);
    } catch (err) {
      setDeleteError(describeError(err));
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <Button asChild variant="ghost" size="icon" aria-label="Back to agent">
          <Link to={`/agents/${agentId}`}>
            <ArrowLeft />
          </Link>
        </Button>
        <div className="min-w-0 flex-1">
          <div className="text-muted-foreground truncate text-xs">
            {agent.data?.name ?? "Agent"} · run #{runId} · started {formatDate(run.data.created_at)}
          </div>
          <div className="truncate text-sm font-medium">{run.data.title ?? "New run"}</div>
        </div>
        <div className="bg-muted flex rounded-md p-0.5 text-sm" role="tablist">
          {(["conversation", "events"] as const).map((t) => (
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
        <Button variant="ghost" size="icon" onClick={onDelete} aria-label="Delete run">
          <Trash2 />
        </Button>
      </header>
      {deleteError && (
        <div className="px-4 pt-2">
          <ErrorBanner message={deleteError} onDismiss={() => setDeleteError(undefined)} />
        </div>
      )}
      {/* Keep the chat mounted while viewing the log so an in-flight stream isn't dropped. */}
      <div className={cn("min-h-0 flex-1", tab !== "conversation" && "hidden")}>
        <RunChat
          key={runId}
          run={run.data}
          onFinish={() => {
            run.reload();
            reloadList();
          }}
        />
      </div>
      {tab === "events" && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <EventLog messages={run.data.messages} />
        </div>
      )}
    </div>
  );
}

function RunChat({ run, onFinish }: { run: Run; onFinish: () => void }) {
  const [transport] = useState(
    () =>
      new DefaultChatTransport({
        api: `/api/agents/${run.agent_id}/runs/${run.id}/chat`,
        credentials: "same-origin",
        // The server keeps the run's history; send only the new message.
        prepareSendMessagesRequest: ({ id, messages, trigger }) => ({
          body: { id, trigger, messages: messages.slice(-1) },
        }),
      }),
  );
  const { messages, sendMessage, status, stop, error } = useChat({
    id: `run-${run.id}`,
    messages: run.ui_messages,
    transport,
    onFinish,
  });
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const busy = status === "submitted" || status === "streaming";

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function submit() {
    const text = input.trim();
    if (!text || busy) return;
    sendMessage({ text });
    setInput("");
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
          {messages.length === 0 && (
            <p className="text-muted-foreground mt-24 text-center">
              Send a message to start this run.
            </p>
          )}
          {messages.map((message) => (
            <MessageView key={message.id} message={message} />
          ))}
          {status === "submitted" && <p className="text-muted-foreground text-sm">Thinking…</p>}
          {error && <p className="text-destructive text-sm">Something went wrong: {error.message}</p>}
          <div ref={bottomRef} />
        </div>
      </div>
      <form
        className="mx-auto w-full max-w-3xl px-4 pb-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="relative">
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Message this agent…"
            className="max-h-48 resize-none pr-12"
            rows={2}
          />
          {busy ? (
            <Button type="button" size="icon" className="absolute right-2 bottom-2 size-8" onClick={stop}>
              <Square />
            </Button>
          ) : (
            <Button type="submit" size="icon" className="absolute right-2 bottom-2 size-8" disabled={!input.trim()}>
              <ArrowUp />
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}

const markdownClass =
  "prose-sm leading-relaxed [&_code]:text-sm [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_pre]:overflow-x-auto [&_ul]:list-disc [&_ul]:pl-5";

function MessageView({ message }: { message: UIMessage }) {
  return (
    <div
      className={cn(
        "flex flex-col gap-2",
        message.role === "user" && "bg-secondary max-w-[80%] self-end rounded-2xl px-4 py-2",
      )}
    >
      {message.parts.map((part, i) => {
        if (part.type === "text") {
          return (
            <div key={i} className={markdownClass}>
              <Markdown>{part.text}</Markdown>
            </div>
          );
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
            toolName?: string;
            state?: string;
            input?: unknown;
            output?: unknown;
            errorText?: string;
          };
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

// --- Event log: the stored Pydantic AI history, request by request ------------------------------

function EventLog({ messages }: { messages: ModelMessage[] }) {
  if (messages.length === 0) return <Placeholder>Nothing has happened in this run yet.</Placeholder>;

  const responses = messages.filter((m) => m.kind === "response");
  const toolCalls = responses.flatMap((m) => m.parts).filter((p) => p.part_kind === "tool-call");
  const tokens = (key: "input_tokens" | "output_tokens") =>
    responses.reduce((sum, m) => sum + (m.usage?.[key] ?? 0), 0);

  // Instructions are attached to every request; only show them when they change.
  let lastInstructions: string | null | undefined;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ["Model calls", responses.length],
          ["Tool calls", toolCalls.length],
          ["Input tokens", tokens("input_tokens")],
          ["Output tokens", tokens("output_tokens")],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md border px-3 py-2">
            <dt className="text-muted-foreground text-xs">{label}</dt>
            <dd className="text-lg font-semibold tabular-nums">{value.toLocaleString()}</dd>
          </div>
        ))}
      </dl>
      <ol className="flex flex-col gap-3">
        {messages.map((message, i) => {
          const showInstructions =
            message.kind === "request" &&
            !!message.instructions &&
            message.instructions !== lastInstructions;
          if (message.kind === "request") lastInstructions = message.instructions;
          return (
            <li key={i} className="rounded-md border">
              <div className="bg-muted/40 flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-xs">
                <Badge variant={message.kind === "request" ? "outline" : "secondary"}>
                  {message.kind === "request" ? "Request" : "Response"}
                </Badge>
                {message.model_name && <span className="font-mono">{message.model_name}</span>}
                {message.kind === "response" && message.usage && (
                  <span className="text-muted-foreground">
                    {message.usage.input_tokens ?? 0} in / {message.usage.output_tokens ?? 0} out
                  </span>
                )}
                {message.timestamp && (
                  <span className="text-muted-foreground ml-auto">
                    {new Date(message.timestamp).toLocaleTimeString()}
                  </span>
                )}
              </div>
              <div className="flex flex-col gap-2 p-3">
                {showInstructions && (
                  <details className="text-xs">
                    <summary className="text-muted-foreground cursor-pointer">Instructions</summary>
                    <pre className="bg-muted/50 mt-1 max-h-64 overflow-auto rounded p-2 whitespace-pre-wrap">
                      {message.instructions}
                    </pre>
                  </details>
                )}
                {message.parts.map((part, j) => (
                  <EventPart key={j} part={part} />
                ))}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function EventPart({ part }: { part: ModelMessage["parts"][number] }) {
  const p = part as Record<string, unknown> & { part_kind: string };
  switch (p.part_kind) {
    case "user-prompt":
    case "system-prompt":
    case "text":
    case "thinking":
      return (
        <div className="text-sm">
          <div className="text-muted-foreground mb-0.5 text-xs">{p.part_kind}</div>
          <p className="whitespace-pre-wrap">
            {typeof p.content === "string" ? p.content : JSON.stringify(p.content)}
          </p>
        </div>
      );
    case "tool-call":
      return (
        <ToolCall name={String(p.tool_name)} state="call" input={parseArgs(p.args)} output={undefined} />
      );
    case "tool-return":
      return (
        <ToolCall
          name={String(p.tool_name)}
          state={p.outcome === "failed" ? "failed" : "return"}
          failed={p.outcome === "failed"}
          input={undefined}
          output={p.content}
        />
      );
    case "retry-prompt":
      return (
        <ToolCall
          name={String(p.tool_name ?? "retry")}
          state="retry"
          failed
          input={undefined}
          output={p.content}
        />
      );
    default:
      return (
        <pre className="bg-muted/50 overflow-auto rounded p-2 text-xs">
          {JSON.stringify(p, null, 2)}
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
