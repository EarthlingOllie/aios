import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

/** Renders markdown, with GitHub extensions (tables, task lists, strikethrough, autolinks). */
export function MarkdownView({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("markdown", className)}>
      <Markdown remarkPlugins={[remarkGfm]}>{children}</Markdown>
    </div>
  );
}
