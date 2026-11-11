import { usePoll, useLoad, useVisible } from "@/components/crud";
import { api } from "@/lib/api";
import { jevNotice } from "@/lib/verdicts";

/** How often Jev's health is read again while the page is visible. */
const JEV_POLL_MS = 60_000;

/**
 * On every page of an organisation while Jev, the prompt-injection check, is down: why new
 * documents wait and agents' tools fail (`GET /api/jev-status`). Nothing while it's up, or its
 * health can't be read.
 */
export function JevNotice() {
  const { data, reload } = useLoad(api.jevStatus);
  usePoll(useVisible(), reload, JEV_POLL_MS);
  const notice = jevNotice(data?.up ?? null);
  if (!notice) return null;
  return (
    <div
      role="status"
      aria-label="Jev is down"
      className="border-destructive/30 bg-destructive/10 border-b px-4 py-2 text-sm"
    >
      {notice}
    </div>
  );
}
