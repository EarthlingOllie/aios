import type { LucideIcon } from "lucide-react";

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
    <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
      <Icon className="text-muted-foreground size-8" />
      <h1 className="text-lg font-semibold">{title}</h1>
      <p className="text-muted-foreground max-w-sm text-sm">{description}</p>
      <p className="text-muted-foreground text-xs">Coming soon.</p>
    </div>
  );
}
