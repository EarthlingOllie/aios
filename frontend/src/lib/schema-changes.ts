import type { SchemaChangeRequest } from "@/lib/api";

export type RequestSummary = {
  label: string;
  variant: "secondary" | "outline" | "destructive";
  /** What happened, in a sentence, for the card. */
  message: string;
  /** Whether the platform is still working on it: worth asking again shortly. */
  inFlight: boolean;
};

/** How a schema change request stands, as its card and the Changes list show it. */
export function requestSummary(request: SchemaChangeRequest): RequestSummary {
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
        message: "Approved. The change is being applied to your data store.",
        inFlight: true,
      };
    case "applied":
      return {
        label: "Applied",
        variant: "outline",
        message: resuming
          ? "Applied. The agent is carrying on."
          : "Applied to your data store.",
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
        message: `Your data store changed after the agent asked, so this no longer applies: ${
          request.error ?? ""
        }`.trim(),
        inFlight: resuming,
      };
    default:
      return {
        label: request.status === "expired" ? "Expired" : "Cancelled",
        variant: "outline",
        message: "It wasn't applied. Nothing changed.",
        inFlight: false,
      };
  }
}
