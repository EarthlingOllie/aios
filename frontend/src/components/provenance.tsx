import type { Provenance, User } from "@/lib/api";
import { useCurrentUser } from "@/lib/current-user";
import { cn } from "@/lib/utils";
import { describeProvenance, personName } from "@/lib/provenance";

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
