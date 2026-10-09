import { useEffect, useRef, useState } from "react";
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { ArrowUp, Search, Square } from "lucide-react";
import Markdown from "react-markdown";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

const transport = new DefaultChatTransport({ api: "/api/chat", credentials: "same-origin" });

export function Chat() {
  const { messages, sendMessage, status, stop, error } = useChat({ transport });
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
              Ask anything — answers are grounded in your uploaded documents.
            </p>
          )}
          {messages.map((message) => (
            <div
              key={message.id}
              className={cn(
                "flex flex-col gap-2",
                message.role === "user" &&
                  "bg-secondary self-end rounded-2xl px-4 py-2 max-w-[80%]",
              )}
            >
              {message.parts.map((part, i) => {
                if (part.type === "text") {
                  return (
                    <div key={i} className="prose-sm leading-relaxed [&_p]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_pre]:overflow-x-auto [&_code]:text-sm">
                      <Markdown>{part.text}</Markdown>
                    </div>
                  );
                }
                if (part.type.startsWith("tool-")) {
                  const query =
                    "input" in part && part.input && typeof part.input === "object"
                      ? (part.input as { query?: string }).query
                      : undefined;
                  return (
                    <div key={i} className="text-muted-foreground flex items-center gap-2 text-xs">
                      <Search className="size-3" />
                      Searched documents{query ? ` for “${query}”` : ""}
                    </div>
                  );
                }
                return null;
              })}
            </div>
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
            placeholder="Message aios…"
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
