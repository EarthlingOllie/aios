import { useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";

/**
 * Live updates: the API's server-sent events streams (a chat's, and the person's), read with
 * `fetch` so a refusal's status is seen. A stream that ends is opened again after the server's
 * `retry`, resuming after the last event id it was given (`lastEventId`). One the server refuses
 * for now (a 503: Redis down, or too many streams) leaves the caller reading every
 * `FALLBACK_READ_MS` (`live` false) and is tried again after its retry hint, until one opens.
 * `gone` closes it for good, with the reason; so does a 404 or 401.
 */

/** How often a screen reads what's new while its stream can't open (ms). */
export const FALLBACK_READ_MS = 5000;
/** How long before opening a stream again when the server gave no hint (ms). */
export const DEFAULT_RETRY_MS = 3000;

export type StreamEvent = { event: string; data: unknown; id: number | null };

/**
 * The complete events in `text` and what's left of an incomplete one, with the last `retry`
 * given (ms), if any. Comments and blank events are skipped; a `data` that isn't JSON is kept as
 * text.
 */
export function parseEvents(text: string): { events: StreamEvent[]; rest: string; retry: number | null } {
  const blocks = text.replace(/\r\n?/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: StreamEvent[] = [];
  let retry: number | null = null;
  for (const block of blocks) {
    let event: string | null = null;
    let data: string | null = null;
    let id: number | null = null;
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const name = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (name === "event") event = value;
      else if (name === "data") data = data === null ? value : `${data}\n${value}`;
      else if (name === "id" && /^\d+$/.test(value)) id = Number(value);
      else if (name === "retry" && /^\d+$/.test(value)) retry = Number(value);
    }
    if (event === null && data === null) continue;
    let parsed: unknown = data;
    try {
      parsed = data === null ? null : JSON.parse(data);
    } catch {
      // Kept as the text it is.
    }
    events.push({ event: event ?? "message", data: parsed, id });
  }
  return { events, rest, retry };
}

/** How long a refusal asks to wait before trying again (ms): its `Retry-After`, if it gives one. */
export function retryAfter(res: Response, fallback: number): number {
  const seconds = Number(res.headers.get("Retry-After"));
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : fallback;
}

export type StreamHandlers = {
  /** Each event, as it arrives. */
  onEvent: (event: StreamEvent) => void;
  /** The id to resume after, asked each time the stream opens (the newest shown, say). */
  lastEventId?: () => number | null;
  /** Each time a stream opens: what was missed while it wasn't is the caller's to read. */
  onOpen?: () => void;
};

export type StreamState = {
  /** Whether a stream is open; false while it can't (read every `FALLBACK_READ_MS` meanwhile). */
  live: boolean;
  /** Why the server closed it for good, if it has (`gone`, a 404): it isn't opened again. */
  gone: string | null;
};

/**
 * Keep `url`'s stream open while mounted (none while `url` is null), handing its events to
 * `handlers`. See the module comment.
 */
export function useStream(url: string | null, handlers: StreamHandlers): StreamState {
  const [state, setState] = useState<StreamState>({ live: true, gone: null });
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    if (url === null) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    setState({ live: true, gone: null });

    const stop = (reason: string) => {
      stopped = true;
      setState({ live: false, gone: reason });
    };
    const later = (ms: number) =>
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, ms);
      });

    async function run() {
      let retry = DEFAULT_RETRY_MS;
      while (!stopped && !abort.signal.aborted) {
        const last = latest.current.lastEventId?.() ?? null;
        const target = last === null ? url! : `${url}${url!.includes("?") ? "&" : "?"}last_event_id=${last}`;
        let res: Response;
        try {
          res = await apiFetch(target, { headers: { Accept: "text/event-stream" }, signal: abort.signal });
        } catch {
          if (abort.signal.aborted) return;
          setState((s) => ({ ...s, live: false }));
          await later(retry);
          continue;
        }
        if (res.status === 404 || res.status === 401 || res.status === 403) {
          stop(res.status === 404 ? "not_found" : "not_allowed");
          return;
        }
        if (!res.ok || !res.body) {
          setState((s) => ({ ...s, live: false }));
          await later(retryAfter(res, retry));
          continue;
        }
        setState({ live: true, gone: null });
        latest.current.onOpen?.();
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        try {
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            const parsed = parseEvents(buffer + decoder.decode(value, { stream: true }));
            buffer = parsed.rest;
            if (parsed.retry !== null) retry = parsed.retry;
            for (const event of parsed.events) {
              if (event.event === "gone") {
                const reason = (event.data as { reason?: string } | null)?.reason ?? "gone";
                stop(reason);
                await reader.cancel().catch(() => undefined);
                return;
              }
              if (event.event !== "ping") latest.current.onEvent(event);
            }
          }
        } catch {
          if (abort.signal.aborted) return;
        }
        // It ended (the server restarting, or Redis gone): open it again shortly.
        await later(retry);
      }
    }

    void run();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [url]);

  return state;
}
