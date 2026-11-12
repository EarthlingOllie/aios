import type { Verdict } from "@/lib/api";

export type VerdictView = {
  label: string;
  variant: "outline" | "secondary" | "destructive";
  /** What it means for the document, and Jev's reason for a flagged one. */
  detail: string;
};

/**
 * How a document's Jev verdict shows on the Documents page: Jev checks every document for prompt
 * injection before agents and search can use it (`aios.verdicts`).
 */
export function verdictView(verdict: Verdict, reason?: string | null): VerdictView {
  switch (verdict) {
    case "clean":
      return { label: "Checked", variant: "outline", detail: "Checked by Jev: agents and search can use it." };
    case "awaiting_check":
      return {
        label: "Awaiting check",
        variant: "secondary",
        detail: "Waiting for Jev to check it for prompt injection: it isn't searchable until then.",
      };
    case "flagged":
      return {
        label: "Flagged",
        variant: "destructive",
        detail: `${reason ?? "Jev judged this a likely prompt injection."} It's kept, but agents and search can't use it unless an admin releases it.`,
      };
    case "released":
      return {
        label: "Released",
        variant: "outline",
        detail: "Flagged by Jev, then released by an admin: agents and search can use it, as untrusted content.",
      };
    case "derived":
      return { label: "Derived", variant: "outline", detail: "Written by an agent: searchable, as untrusted content." };
  }
}

/** What everyone is told while Jev is down (`up` false); nothing while it's up or unknown. */
export function jevNotice(up: boolean | null): string | null {
  if (up !== false) return null;
  return "Jev, the prompt-injection check, is down. New documents wait to be checked before they're searchable, and agents' code and data tools return errors until it's back.";
}

/** What a chat's composer says while Jev is down (`up` false): every message a person posts is
 * checked by Jev first, so none can be posted. Nothing while it's up or unknown. */
export function composerJevNotice(up: boolean | null): string | null {
  if (up !== false) return null;
  return "Messages can't be checked right now: Jev, the prompt-injection check, is down, so posting won't work until it's back.";
}
