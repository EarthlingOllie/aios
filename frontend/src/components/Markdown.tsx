import { memo, useMemo } from "react";
import Markdown, { type Components, type Options } from "react-markdown";
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
  remarkPlugins: extraPlugins,
  components,
}: {
  children: string;
  className?: string;
  /** Run after GitHub's extensions, e.g. to mark a chat message's mentions. Keep it stable. */
  remarkPlugins?: Options["remarkPlugins"];
  /** Stand-ins for HTML elements, e.g. to show those marks. Keep it stable. */
  components?: Components;
}) {
  const plugins = useMemo(() => [...remarkPlugins, ...(extraPlugins ?? [])], [extraPlugins]);
  return (
    <div className={cn("markdown", className)}>
      <Markdown remarkPlugins={plugins} components={components}>
        {children}
      </Markdown>
    </div>
  );
});
