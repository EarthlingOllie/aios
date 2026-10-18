import { useState, type ChangeEvent, type KeyboardEvent, type SyntheticEvent } from "react";
import { ArrowUp, Bot, Square, User, Workflow, type LucideIcon } from "lucide-react";

import { MarkdownEditor } from "@/components/MarkdownEditor";
import { Button } from "@/components/ui/button";
import type { ChatMemberKind } from "@/lib/api";
import {
  filterMentionable,
  insertMention,
  mentionQuery,
  mentionToken,
  type Mentionable,
  type Mentionables,
  type MentionQuery,
} from "@/lib/mentions";
import { cn } from "@/lib/utils";

/** The icons the navigation uses for agents and workflows, and one for users. */
export const MEMBER_KIND: Record<ChatMemberKind, { label: string; icon: LucideIcon }> = {
  user: { label: "User", icon: User },
  agent: { label: "Agent", icon: Bot },
  workflow: { label: "Workflow", icon: Workflow },
};

const GROUPS = [
  ["users", "Users"],
  ["agents", "Agents"],
  ["workflows", "Workflows"],
] as const;

/**
 * The message box under a chat: Enter sends, Shift+Enter adds a line, and the button stops a reply.
 * Given `mentionable`, typing `@` offers those members, grouped by kind, to mention: arrows move,
 * Enter or Tab inserts the one picked (as a token, see `mentionToken`), and Escape closes the list.
 */
export function ChatComposer({
  id,
  placeholder,
  value,
  onChange,
  busy,
  disabled = false,
  onStop,
  onSubmit,
  mentionable,
}: {
  id: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  busy: boolean;
  /** Nothing can be sent (e.g. the run waits on a decision). */
  disabled?: boolean;
  onStop: () => void;
  onSubmit: () => void;
  /** Who `@` offers to mention; no autocomplete without it. */
  mentionable?: Mentionables;
}) {
  // The `@` word at the caret, and whether Escape closed the list for it.
  const [at, setAt] = useState<MentionQuery | null>(null);
  const [dismissed, setDismissed] = useState<number | null>(null);
  const [active, setActive] = useState(0);

  const groups = mentionable && at ? filterMentionable(mentionable, at.query) : undefined;
  const options = groups ? GROUPS.flatMap(([key]) => groups[key]) : [];
  const open = at !== null && dismissed !== at.start && options.length > 0;
  const listId = `${id}-mentions`;
  const optionId = (m: Mentionable) => `${listId}-${m.kind}-${m.id}`;

  /** Follows the caret: what's being typed after an `@`, if anything. */
  function track(el: HTMLTextAreaElement) {
    if (!mentionable) return;
    const next = el.selectionStart === el.selectionEnd ? mentionQuery(el.value, el.selectionStart) : null;
    if (next?.start !== at?.start || next?.query !== at?.query) setActive(0);
    if (next?.start !== at?.start) setDismissed(null);
    setAt(next);
  }

  function choose(member: Mentionable) {
    const el = document.getElementById(id) as HTMLTextAreaElement | null;
    if (!el || !at) return;
    el.focus();
    el.setSelectionRange(at.start, at.start + 1 + at.query.length);
    // As the toolbar does, so the insertion is one step on the browser's undo stack.
    if (!document.execCommand("insertText", false, `${mentionToken(member)} `)) {
      const { text, caret } = insertMention(el.value, at, member);
      onChange(text);
      requestAnimationFrame(() => el.setSelectionRange(caret, caret));
    }
    setAt(null);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (open) {
      const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (step) {
        e.preventDefault();
        setActive((active + step + options.length) % options.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        choose(options[Math.min(active, options.length - 1)]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setDismissed(at.start);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSubmit();
    }
  }

  const activeOption = open ? options[Math.min(active, options.length - 1)] : undefined;
  return (
    <form
      className="relative mx-auto w-full max-w-3xl px-4 pb-4"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      {open && groups && (
        <div
          id={listId}
          role="listbox"
          aria-label="Mention"
          className="bg-popover text-popover-foreground absolute bottom-full left-4 z-10 mb-1 max-h-64 w-72 overflow-y-auto rounded-md border p-1 shadow-md"
        >
          {GROUPS.filter(([key]) => groups[key].length > 0).map(([key, title]) => (
            <div key={key} role="group" aria-label={title}>
              <div role="presentation" className="text-muted-foreground px-2 pt-1.5 pb-0.5 text-xs font-medium">
                {title}
              </div>
              {groups[key].map((member) => {
                const { label, icon: Icon } = MEMBER_KIND[member.kind];
                const selected = member === activeOption;
                return (
                  <div
                    key={optionId(member)}
                    id={optionId(member)}
                    role="option"
                    aria-selected={selected}
                    className={cn(
                      "flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm",
                      selected && "bg-accent",
                    )}
                    // Keep the focus (and caret) in the textarea.
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseEnter={() => setActive(options.indexOf(member))}
                    onClick={() => choose(member)}
                  >
                    <Icon className="text-muted-foreground size-4 shrink-0" aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{member.name}</span>{" "}
                    <span className="text-muted-foreground text-xs">{label.toLowerCase()}</span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
      <MarkdownEditor
        id={id}
        value={value}
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => {
          onChange(e.target.value);
          track(e.target);
        }}
        onSelect={(e: SyntheticEvent<HTMLTextAreaElement>) => track(e.currentTarget)}
        onBlur={() => setAt(null)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        aria-label="Message"
        {...(mentionable && {
          "aria-autocomplete": "list" as const,
          "aria-controls": open ? listId : undefined,
          "aria-activedescendant": activeOption && optionId(activeOption),
        })}
        className="max-h-48 resize-none pr-12"
        previewClassName="max-h-48 pr-12"
        rows={2}
      >
        {busy ? (
          <Button type="button" size="icon" className="absolute right-2 bottom-2 size-8" onClick={onStop}>
            <Square />
          </Button>
        ) : (
          <Button type="submit" size="icon" className="absolute right-2 bottom-2 size-8" disabled={disabled || !value.trim()}>
            <ArrowUp />
          </Button>
        )}
      </MarkdownEditor>
    </form>
  );
}
