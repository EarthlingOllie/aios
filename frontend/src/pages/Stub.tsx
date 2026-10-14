import type { LucideIcon } from "lucide-react";

import { Placeholder } from "@/components/crud";

// Placeholder for a nav section that has a route but no page yet.
export function StubPage({
  title,
  icon: Icon,
  description,
}: {
  title: string;
  icon: LucideIcon;
  description: string;
}) {
  return (
    <Placeholder>
      <div className="flex flex-col items-center gap-2 text-center">
        <Icon className="size-8" />
        <h1 className="text-foreground text-lg font-semibold">{title}</h1>
        <p className="max-w-sm">{description}</p>
        <p className="text-xs">Coming soon.</p>
      </div>
    </Placeholder>
  );
}
