import { ArrowUp, Square } from "lucide-react";

import { MarkdownEditor } from "@/components/MarkdownEditor";
import { Button } from "@/components/ui/button";

/** The message box under a chat: Enter sends, Shift+Enter adds a line, and the button stops a reply. */
export function ChatComposer({
  id,
  placeholder,
  value,
  onChange,
  busy,
  disabled = false,
  onStop,
  onSubmit,
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
}) {
  return (
    <form
      className="mx-auto w-full max-w-3xl px-4 pb-4"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <MarkdownEditor
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSubmit();
          }
        }}
        placeholder={placeholder}
        aria-label="Message"
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
