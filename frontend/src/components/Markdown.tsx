import { memo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

const remarkPlugins = [remarkGfm];

/**
 * Renders markdown, with GitHub extensions (tables, task lists, strikethrough, autolinks).
 * Memoised so a streaming chat reply doesn't re-parse every earlier message on each token.
 */
export const MarkdownView = memo(function MarkdownView({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={cn("markdown", className)}>
      <Markdown remarkPlugins={remarkPlugins}>{children}</Markdown>
    </div>
  );
});
