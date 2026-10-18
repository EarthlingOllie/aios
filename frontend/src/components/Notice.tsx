import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * A bordered line of news: progress or a wait (a status, with a spinner while `busy`), or an
 * error (an alert). `label` names it for screen readers and tests.
 */
export function Notice({
  label,
  error = false,
  busy = false,
  className,
  children,
}: {
  label: string;
  error?: boolean;
  busy?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      role={error ? "alert" : "status"}
      aria-label={label}
      className={cn(
        "flex items-start gap-2 rounded-md border text-sm",
        error ? "border-destructive/50 text-destructive" : "text-muted-foreground",
        className,
      )}
    >
      {busy && <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" />}
      <span>{children}</span>
    </div>
  );
}
