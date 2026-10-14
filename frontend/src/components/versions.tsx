import { useRef, useState } from "react";
import { Link } from "react-router";
import { History } from "lucide-react";

import { formatDate } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { diffLines } from "@/lib/diff";
import { cn } from "@/lib/utils";

export function VersionBadge({ version, className }: { version: number; className?: string }) {
  return (
    <Badge variant="outline" className={cn("font-mono", className)}>
      v{version}
    </Badge>
  );
}

/** "Restore this version", disabled while `onRestore` runs so a double click restores once. */
export function RestoreButton({ onRestore }: { onRestore: () => Promise<void> }) {
  const [restoring, setRestoring] = useState(false);
  // State only updates on the next render; this also stops a click that lands before then.
  const inFlight = useRef(false);
  async function onClick() {
    if (inFlight.current) return;
    inFlight.current = true;
    setRestoring(true);
    try {
      await onRestore();
    } finally {
      inFlight.current = false;
      setRestoring(false);
    }
  }
  return (
    <Button size="sm" variant="outline" onClick={onClick} disabled={restoring}>
      <History />
      {restoring ? "Restoring…" : "Restore this version"}
    </Button>
  );
}

export function VersionHistory({
  versions,
  error,
  current,
  href,
  detail,
}: {
  versions: { version: number; note: string | null; created_at: string }[] | undefined;
  /** Why the versions couldn't be loaded, if they couldn't. */
  error?: string;
  current: number;
  href: (version: number) => string;
  detail?: (version: number) => string | undefined;
}) {
  if (error) return <p className="text-destructive text-sm">{error}</p>;
  if (!versions) return null;
  return (
    <ol className="divide-y rounded-md border">
      {versions.map((v) => (
        <li key={v.version}>
          <Link to={href(v.version)} className="hover:bg-accent flex items-center gap-3 px-3 py-2">
            <VersionBadge version={v.version} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm">
                {v.note ?? <span className="text-muted-foreground">No note</span>}
              </span>
              <span className="text-muted-foreground block text-xs">
                {formatDate(v.created_at)}
                {detail?.(v.version) && ` · ${detail(v.version)}`}
              </span>
            </span>
            {v.version === current && <Badge variant="secondary">current</Badge>}
          </Link>
        </li>
      ))}
    </ol>
  );
}

/** A unified line diff between two texts. */
export function TextDiff({ before, after }: { before: string; after: string }) {
  const lines = diffLines(before, after);
  if (lines.every((l) => l.kind === "same")) {
    return <p className="text-muted-foreground text-sm">No differences.</p>;
  }
  return (
    <pre className="max-h-[32rem] overflow-auto rounded-md border text-xs">
      {lines.map((line, i) => (
        <div
          key={i}
          className={cn(
            "px-3 whitespace-pre-wrap",
            line.kind === "add" && "bg-emerald-500/15 text-emerald-800 dark:text-emerald-300",
            line.kind === "remove" && "bg-red-500/15 text-red-800 line-through dark:text-red-300",
          )}
        >
          <span className="text-muted-foreground mr-2 select-none">
            {line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " "}
          </span>
          {line.text || " "}
        </div>
      ))}
    </pre>
  );
}

/** "Compare with" picker plus the diff; `versions` are the candidate base versions. */
export function CompareVersions({
  versions,
  error,
  base,
  onBaseChange,
  before,
  after,
}: {
  versions: number[];
  /** Why the versions couldn't be loaded, if they couldn't. */
  error?: string;
  base: number | undefined;
  onBaseChange: (version: number) => void;
  before: string | undefined;
  after: string;
}) {
  if (error) return <p className="text-destructive text-sm">{error}</p>;
  if (versions.length === 0) {
    return <p className="text-muted-foreground text-sm">This is the first version.</p>;
  }
  return (
    <div className="flex flex-col gap-2">
      <label className="text-muted-foreground flex items-center gap-2 text-sm">
        Compare with
        <select
          className="border-input rounded-md border bg-transparent px-2 py-1 text-sm"
          value={base}
          onChange={(e) => onBaseChange(Number(e.target.value))}
        >
          {versions.map((v) => (
            <option key={v} value={v}>
              v{v}
            </option>
          ))}
        </select>
      </label>
      {before !== undefined && <TextDiff before={before} after={after} />}
    </div>
  );
}
