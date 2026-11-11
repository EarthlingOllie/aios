import type { SchemaChangeRequest } from "@/lib/api";
import { aRole } from "@/lib/permissions";

export type RequestSummary = {
  label: string;
  variant: "secondary" | "outline" | "destructive";
  /** What happened, in a sentence, for the card. */
  message: string;
  /** Whether the platform is still working on it: worth asking again shortly. */
  inFlight: boolean;
  /** What its `decided_at` marks: a decision, or its expiry or cancellation. */
  decidedLabel: "Decided" | "Expired" | "Cancelled";
};

/** How a schema change request stands, as its card and the Changes list show it. */
export function requestSummary(request: SchemaChangeRequest): RequestSummary {
  const decidedLabel =
    request.status === "expired" ? "Expired" : request.status === "cancelled" ? "Cancelled" : "Decided";
  return { ...describe(request), decidedLabel };
}

function describe(request: SchemaChangeRequest): Omit<RequestSummary, "decidedLabel"> {
  const resuming = !request.resumed_at && !!request.run;
  switch (request.status) {
    case "pending":
      return {
        label: "Waiting for you",
        variant: "secondary",
        message: "The agent is waiting for you to approve or reject this change.",
        inFlight: false,
      };
    case "approved":
      return {
        label: "Applying",
        variant: "secondary",
        message: "Approved. The change is being applied to the organisation's data store.",
        inFlight: true,
      };
    case "applied":
      return {
        label: "Applied",
        variant: "outline",
        message: resuming
          ? "Applied. The agent is carrying on."
          : "Applied to the organisation's data store.",
        inFlight: resuming,
      };
    case "rejected":
      return {
        label: "Rejected",
        variant: "outline",
        message:
          (request.note ? `Rejected: ${request.note}` : "Rejected. Nothing changed.") +
          (resuming ? " The agent is carrying on." : ""),
        inFlight: resuming,
      };
    case "failed":
      return {
        label: "Failed",
        variant: "destructive",
        message: `Applying it failed, and nothing changed: ${request.error ?? "unknown error"}`,
        inFlight: resuming,
      };
    case "stale":
      return {
        label: "No longer applies",
        variant: "destructive",
        message: `The organisation's data store changed after the agent asked, so this no longer applies: ${
          request.error ?? ""
        }`.trim(),
        inFlight: resuming,
      };
    case "expired":
      return {
        label: "Expired",
        variant: "outline",
        message:
          "Nobody decided in time, so it expired, which counts as rejected. Nothing changed." +
          (resuming ? " The agent is carrying on." : ""),
        inFlight: resuming,
      };
    case "cancelled":
      return {
        label: "Cancelled",
        variant: "outline",
        message: "Cancelled: the run that asked was deleted or stopped. Nothing changed.",
        inFlight: false,
      };
  }
}

/** Who may decide a pending request, by the approval rules: "Needs an admin to approve". Null
 * once it's decided. */
export function approvalNeeds(request: SchemaChangeRequest): string | null {
  if (request.status !== "pending") return null;
  const who = request.needs_role === "admin" ? aRole("admin") : `${aRole("editor")} or ${aRole("admin")}`;
  return `Needs ${who} to approve`;
}
