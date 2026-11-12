import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
  type RefObject,
} from "react";

import { api } from "@/lib/api";
import { useStream, type StreamEvent, type StreamState } from "@/lib/stream";

/**
 * The person's stream (`/api/me/stream`): one per page, however many parts of it listen (the
 * chat list, the bell). Each listener hears every event, and each time it opens.
 */

type Listener = {
  onEvent: (event: StreamEvent) => void;
  /** Each time it opens: what was missed while it wasn't is the listener's to read. */
  onOpen?: () => void;
};

type PersonStream = {
  subscribe: (listener: RefObject<Listener>) => () => void;
  state: StreamState;
};

const PersonStreamContext = createContext<PersonStream | null>(null);

export function PersonStreamProvider({ children }: { children: ReactNode }) {
  const listeners = useRef(new Set<RefObject<Listener>>());
  const state = useStream(api.personStreamUrl(), {
    onEvent: (event) => listeners.current.forEach((l) => l.current.onEvent(event)),
    onOpen: () => listeners.current.forEach((l) => l.current.onOpen?.()),
  });
  const subscribe = useCallback((listener: RefObject<Listener>) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);
  const value = useMemo(() => ({ subscribe, state }), [subscribe, state]);
  return <PersonStreamContext.Provider value={value}>{children}</PersonStreamContext.Provider>;
}

const NOT_OPEN: StreamState = { live: false, gone: null };

/**
 * Hear the person's stream's events while mounted; whether it's open (`live`). Outside a
 * `PersonStreamProvider` it's never open, so callers read on their fallback interval.
 */
export function usePersonStream(listener: Listener): StreamState {
  const context = useContext(PersonStreamContext);
  const latest = useRef(listener);
  latest.current = listener;
  useEffect(() => context?.subscribe(latest), [context]);
  return context?.state ?? NOT_OPEN;
}
