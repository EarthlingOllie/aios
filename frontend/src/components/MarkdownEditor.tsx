import { useLayoutEffect, useRef, useState, type ComponentProps, type KeyboardEvent, type ReactNode } from "react";
import "@github/markdown-toolbar-element";
import {
  Bold,
  Code,
  Heading2,
  Italic,
  Link,
  List,
  ListOrdered,
  ListTodo,
  Strikethrough,
  TextQuote,
  type LucideIcon,
} from "lucide-react";

import { MarkdownView } from "@/components/Markdown";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

// The md-* elements are defined by @github/markdown-toolbar-element. Each applies its formatting
// to the <markdown-toolbar>'s `for` textarea with execCommand("insertText"), so the change fires
// a normal input event (React's onChange) and stays on the browser's undo stack.
type ElementProps = { children?: ReactNode; className?: string; title?: string };
declare module "react" {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace JSX {
    interface IntrinsicElements {
      "markdown-toolbar": ElementProps & { for: string; inert?: boolean; ref?: React.Ref<HTMLElement> };
      "md-header": ElementProps & { level?: string };
      "md-bold": ElementProps;
      "md-italic": ElementProps;
      "md-strikethrough": ElementProps;
      "md-code": ElementProps;
      "md-link": ElementProps;
      "md-quote": ElementProps;
      "md-unordered-list": ElementProps;
      "md-ordered-list": ElementProps;
      "md-task-list": ElementProps;
    }
  }
}

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
const mod = isMac ? "⌘" : "Ctrl+";

type ButtonTag = Extract<keyof React.JSX.IntrinsicElements, `md-${string}`>;

type Action = {
  tag: ButtonTag;
  label: string;
  icon: LucideIcon;
  /** ⌘ (Mac) or Ctrl (elsewhere) + this key applies the action from the textarea. */
  key?: string;
  /** Less common; hidden on narrow screens so the toolbar fits. */
  secondary?: boolean;
};

const actions: Action[][] = [
  [
    { tag: "md-header", label: "Heading", icon: Heading2 },
    { tag: "md-bold", label: "Bold", icon: Bold, key: "b" },
    { tag: "md-italic", label: "Italic", icon: Italic, key: "i" },
    { tag: "md-strikethrough", label: "Strikethrough", icon: Strikethrough, secondary: true },
  ],
  [
    { tag: "md-code", label: "Code", icon: Code, key: "e" },
    { tag: "md-link", label: "Link", icon: Link, key: "k" },
    { tag: "md-quote", label: "Quote", icon: TextQuote, secondary: true },
  ],
  [
    { tag: "md-unordered-list", label: "Bulleted list", icon: List },
    { tag: "md-ordered-list", label: "Numbered list", icon: ListOrdered },
    { tag: "md-task-list", label: "Task list", icon: ListTodo, secondary: true },
  ],
];

const shortcuts = new Map(actions.flat().flatMap((a) => (a.key ? [[a.key, a.tag]] : [])));

type Props = Omit<ComponentProps<"textarea">, "id" | "value"> & {
  /** Required: the toolbar finds its textarea by id. */
  id: string;
  value: string;
  /** Rendered inside the editor's frame, after the text (e.g. a send button). */
  children?: ReactNode;
  /** Classes for the frame around the toolbar and text. */
  frameClassName?: string;
  /** Classes for the rendered preview (the textarea's className styles the text). */
  previewClassName?: string;
};

/**
 * A plain textarea with a markdown formatting toolbar, keyboard shortcuts, list continuation and
 * a rendered preview. The value stays plain markdown text.
 */
export function MarkdownEditor({
  id,
  value,
  children,
  className,
  frameClassName,
  previewClassName,
  onKeyDown,
  ...props
}: Props) {
  const toolbarRef = useRef<HTMLElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [preview, setPreview] = useState(false);
  // The preview takes the textarea's height, so toggling doesn't make the page jump. A fixed
  // height (not min-height) lets previewClassName's max-height cap it and the preview scroll.
  const [height, setHeight] = useState<number>();
  const lastValue = useRef(value);

  // When the caller clears the text while it's previewed (e.g. after sending a message), go back
  // to Write and focus it for the next one. Previewing an already empty editor stays put. A layout
  // effect, so the textarea is shown again before the focus runs.
  useLayoutEffect(() => {
    if (preview && lastValue.current && !value) {
      setPreview(false);
      requestAnimationFrame(() => textareaRef.current?.focus());
    }
    lastValue.current = value;
  }, [value, preview]);

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing) return;
    // Only ⌘ on a Mac: there Ctrl+B/E/K are the standard text-editing keys (back, end of line, kill).
    const tag = (isMac ? e.metaKey : e.ctrlKey) && !e.altKey && !e.shiftKey && shortcuts.get(e.key.toLowerCase());
    if (tag) {
      e.preventDefault();
      toolbarRef.current?.querySelector<HTMLElement>(tag)?.click();
      return;
    }
    onKeyDown?.(e);
    // Enter that would insert a newline (the caller didn't take it, e.g. to send) continues a list.
    if (!e.defaultPrevented && e.key === "Enter" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (continueList(e.currentTarget)) e.preventDefault();
    }
  }

  function togglePreview(on: boolean) {
    if (on === preview) return;
    if (on) setHeight(textareaRef.current?.offsetHeight);
    setPreview(on);
    if (!on) requestAnimationFrame(() => textareaRef.current?.focus());
  }

  return (
    <div
      className={cn(
        "border-input focus-within:border-ring focus-within:ring-ring/50 dark:bg-input/30 relative rounded-md border shadow-xs transition-[color,box-shadow] focus-within:ring-[3px]",
        frameClassName,
      )}
    >
      <div className="flex items-center gap-1 border-b px-1.5 py-1">
        <markdown-toolbar
          ref={toolbarRef}
          for={id}
          // Formatting needs the textarea, which the preview hides.
          inert={preview}
          className={cn("flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto", preview && "opacity-40")}
        >
          {actions.map((group, i) => (
            <span key={i} className="flex items-center gap-0.5 not-first:border-l not-first:pl-0.5">
              {group.map(({ tag: Tag, label, icon: Icon, key, secondary }) => (
                <Tag
                  key={label}
                  // md-header's default level is 3; prompts read better with ## sections.
                  {...(Tag === "md-header" ? { level: "2" } : {})}
                  title={key ? `${label} (${mod}${key.toUpperCase()})` : label}
                  className={cn(
                    "text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-ring/50 inline-flex size-7 shrink-0 cursor-pointer items-center justify-center rounded outline-none focus-visible:ring-2",
                    secondary && "max-sm:hidden",
                  )}
                >
                  <Icon className="size-4" aria-hidden />
                  <span className="sr-only">{label}</span>
                </Tag>
              ))}
            </span>
          ))}
        </markdown-toolbar>
        <div className="bg-muted flex shrink-0 rounded p-0.5 text-xs" role="tablist" aria-label="Editor mode">
          {([false, true] as const).map((on) => (
            <button
              key={String(on)}
              type="button"
              role="tab"
              aria-selected={preview === on}
              onClick={() => togglePreview(on)}
              className={cn(
                "rounded px-2 py-0.5",
                preview === on ? "bg-background shadow-xs" : "text-muted-foreground",
              )}
            >
              {on ? "Preview" : "Write"}
            </button>
          ))}
        </div>
      </div>
      <Textarea
        ref={textareaRef}
        id={id}
        value={value}
        onKeyDown={handleKeyDown}
        className={cn(
          "rounded-none border-0 bg-transparent shadow-none focus-visible:ring-0 dark:bg-transparent",
          className,
          preview && "hidden",
        )}
        {...props}
      />
      {preview && (
        <div
          className={cn("overflow-y-auto px-3 py-2", previewClassName)}
          style={{ height }}
          data-testid="markdown-preview"
        >
          {value.trim() ? (
            <MarkdownView>{value}</MarkdownView>
          ) : (
            <p className="text-muted-foreground text-sm">Nothing to preview.</p>
          )}
        </div>
      )}
      {children}
    </div>
  );
}

// Ordered markers are capped at 3 digits (CommonMark allows 9) so a line opening with a year,
// like "1999. It began", isn't taken for a list; lists past item 999 just stop auto-numbering.
const listItem = /^(\s*)(?:([-*+] (?:\[[ xX]\] )?)|(\d{1,3})([.)]) )/;
// "* * *", "- - -", "***": a horizontal rule, not a list item.
const thematicBreak = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;

/**
 * On Enter inside a list item, start the next item ("- ", "- [ ] ", "2. "); on an empty item,
 * end the list instead. With text after the caret the item is split, never treated as empty.
 * Returns whether it handled the key.
 */
function continueList(textarea: HTMLTextAreaElement): boolean {
  const { value, selectionStart: start, selectionEnd: end } = textarea;
  if (start !== end) return false;
  const lineStart = value.lastIndexOf("\n", start - 1) + 1;
  const line = value.slice(lineStart, start);
  const match = listItem.exec(line);
  if (!match || thematicBreak.test(line)) return false;
  const [marker, indent, bullet, number, delimiter] = match;
  const lineEnd = value.indexOf("\n", start);
  const rest = value.slice(start, lineEnd < 0 ? undefined : lineEnd);
  if (line.length === marker.length && !rest) {
    textarea.setSelectionRange(lineStart, start);
    return insertText(textarea, "");
  }
  const next = bullet ? bullet.replace(/\[[xX]\]/, "[ ]") : `${Number(number) + 1}${delimiter} `;
  return insertText(textarea, `\n${indent}${next}`);
}

/** Replaces the selection like typing would: firing input and keeping native undo. */
function insertText(textarea: HTMLTextAreaElement, text: string): boolean {
  textarea.focus();
  if (document.execCommand(text ? "insertText" : "delete", false, text)) return true;
  textarea.setRangeText(text, textarea.selectionStart, textarea.selectionEnd, "end");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
}
