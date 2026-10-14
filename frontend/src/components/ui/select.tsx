import { useId, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type SelectOption = {
  value: string;
  label: string;
  /** Shown, and reachable so it's announced, but can't be chosen. */
  disabled?: boolean;
  /** A short note beside the label, e.g. why it's disabled; announced as its description. */
  badge?: string;
};

export type SelectGroup = { label: string; options: SelectOption[] };

/**
 * A select whose options can carry a badge, which a native `<select>` can't show: a button that
 * opens a listbox (the ARIA "select-only combobox"). Focus stays on the button; the arrow keys,
 * Home and End move through the options, a letter jumps to the next one it starts, Enter or
 * Space chooses, and Escape or leaving closes. Give it a `<label htmlFor={id}>`.
 */
export function Select({
  id,
  value,
  onChange,
  groups,
  placeholder,
  className,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  groups: SelectGroup[];
  /** What the button shows when `value` isn't one of the options. */
  placeholder: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const prefix = useId();
  const options = groups.flatMap((g) => g.options);
  const selected = options.findIndex((o) => o.value === value);
  const optionId = (i: number) => `${prefix}-option-${i}`;

  const show = (i: number) => {
    setOpen(true);
    setActive(i);
    // After rendering, so a newly opened list has its options.
    requestAnimationFrame(() =>
      document.getElementById(optionId(i))?.scrollIntoView({ block: "nearest" }),
    );
  };
  const choose = (i: number) => {
    if (options[i].disabled) return;
    // Like a native select, choosing what's already chosen isn't a change.
    if (options[i].value !== value) onChange(options[i].value);
    setOpen(false);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const last = options.length - 1;
    if (last < 0) return;
    const move = (i: number) => {
      e.preventDefault();
      show(Math.max(0, Math.min(last, i)));
    };
    if (!open) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) move(Math.max(selected, 0));
      return;
    }
    if (e.key === "ArrowDown") move(active + 1);
    else if (e.key === "ArrowUp") move(active - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(last);
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      choose(active);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "Tab") setOpen(false);
    else if (e.key.length === 1) {
      const letter = e.key.toLowerCase();
      for (let step = 1; step <= options.length; step++) {
        const i = (active + step) % options.length;
        if (options[i].label.toLowerCase().startsWith(letter)) return move(i);
      }
    }
  };

  let index = 0;
  return (
    <div className={cn("relative", className)}>
      <button
        type="button"
        id={id}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? `${prefix}-list` : undefined}
        aria-activedescendant={open ? optionId(active) : undefined}
        className="border-input flex w-full items-center gap-2 rounded-md border bg-transparent px-2 py-1.5 text-left text-sm"
        onClick={() => (open ? setOpen(false) : options.length > 0 && show(Math.max(selected, 0)))}
        onKeyDown={onKeyDown}
        onBlur={() => setOpen(false)}
      >
        <span className={cn("flex-1 truncate", selected < 0 && "text-muted-foreground")}>
          {selected < 0 ? placeholder : options[selected].label}
        </span>
        <ChevronDown className="text-muted-foreground size-4 shrink-0" />
      </button>
      {open && options.length > 0 && (
        <div
          id={`${prefix}-list`}
          role="listbox"
          aria-labelledby={id}
          className="bg-popover text-popover-foreground absolute z-50 mt-1 max-h-72 w-full min-w-max overflow-auto rounded-md border p-1 shadow-md"
          // Keep focus on the button, so it can go on handling the keys.
          onMouseDown={(e) => e.preventDefault()}
        >
          {groups.map(
            (group, g) =>
              group.options.length > 0 && (
                <div key={group.label} role="group" aria-labelledby={`${prefix}-group-${g}`}>
                  <div
                    id={`${prefix}-group-${g}`}
                    className="text-muted-foreground px-2 pt-1.5 pb-1 text-xs font-medium"
                  >
                    {group.label}
                  </div>
                  {group.options.map((option) => {
                    const i = index++;
                    return (
                      <div
                        key={option.value}
                        id={optionId(i)}
                        role="option"
                        aria-selected={i === selected}
                        aria-disabled={option.disabled || undefined}
                        aria-labelledby={`${optionId(i)}-label`}
                        aria-describedby={option.badge ? `${optionId(i)}-badge` : undefined}
                        className={cn(
                          "flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm",
                          i === active && "bg-accent text-accent-foreground",
                          option.disabled && "text-muted-foreground",
                        )}
                        onMouseMove={() => i !== active && setActive(i)}
                        onClick={() => choose(i)}
                      >
                        <span id={`${optionId(i)}-label`} className="flex-1">
                          {option.label}
                        </span>
                        {option.badge && (
                          <Badge id={`${optionId(i)}-badge`} variant="outline">
                            {option.badge}
                          </Badge>
                        )}
                        <Check className={cn("size-4", i !== selected && "invisible")} />
                      </div>
                    );
                  })}
                </div>
              ),
          )}
        </div>
      )}
    </div>
  );
}
