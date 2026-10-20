import type { ReactNode } from "react";

import type { Provenance, User } from "@/lib/api";
import { useCurrentUser } from "@/lib/current-user";
import { cn } from "@/lib/utils";
import { describeProvenance, describeStarter, personName, type Started } from "@/lib/provenance";

/** Who made something of the organisation's and who last changed it, as a line of small text;
 * nothing when neither is known. */
export function ProvenanceLine({ of, className }: { of: Provenance; className?: string }) {
  const me = useCurrentUser();
  const line = describeProvenance(of, me);
  if (!line) return null;
  return (
    <span
      data-testid="provenance"
      className={cn("text-muted-foreground block truncate text-xs", className)}
    >
      {line}
    </span>
  );
}

/** " · saved by Sam", for after a version's date; nothing when who saved it isn't known. */
export function useSavedBy(): (user: User | null | undefined) => string {
  const me = useCurrentUser();
  return (user) => (user ? ` · saved by ${personName(user, me)}` : "");
}

/** Who started a run: the member, with their avatar, or that a schedule did; nothing when
 * neither is known. Inline, for a run's line in a list or on its page, between `before` and
 * `after` (separators, say) when it's shown. */
export function StartedBy({
  run,
  before,
  after,
  className,
}: {
  run: Started;
  before?: ReactNode;
  after?: ReactNode;
  className?: string;
}) {
  const me = useCurrentUser();
  const line = describeStarter(run, me);
  if (!line) return null;
  return (
    <>
      {before}
      <span
        data-testid="started-by"
        title={run.created_by ? `@${run.created_by.login}` : undefined}
        className={cn("inline-flex min-w-0 items-center gap-1", className)}
      >
        {run.created_by?.avatar_url && (
          <img src={run.created_by.avatar_url} alt="" className="size-4 shrink-0 rounded-full" />
        )}
        <span className="truncate">{line}</span>
      </span>
      {after}
    </>
  );
}
