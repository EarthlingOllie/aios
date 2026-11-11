import { describe, expect, it } from "vitest";

import { jevNotice, verdictView } from "@/lib/verdicts";

describe("verdictView", () => {
  it("says a document Jev passed is checked and searchable", () => {
    expect(verdictView("clean")).toEqual({
      label: "Checked",
      variant: "outline",
      detail: "Checked by Jev: agents and search can use it.",
    });
  });

  it("says a document waiting for Jev isn't searchable yet", () => {
    expect(verdictView("awaiting_check")).toEqual({
      label: "Awaiting check",
      variant: "secondary",
      detail: "Waiting for Jev to check it for prompt injection: it isn't searchable until then.",
    });
  });

  it("says why a flagged document is kept out, with Jev's reason", () => {
    expect(verdictView("flagged", "Jev judged this a likely prompt injection (probability 0.97).")).toEqual({
      label: "Flagged",
      variant: "destructive",
      detail:
        "Jev judged this a likely prompt injection (probability 0.97). It's kept, but agents and search can't use it unless an admin releases it.",
    });
  });

  it("says a released document is searchable but still treated as untrusted", () => {
    expect(verdictView("released")).toEqual({
      label: "Released",
      variant: "outline",
      detail: "Flagged by Jev, then released by an admin: agents and search can use it, as untrusted content.",
    });
  });
});

describe("jevNotice", () => {
  it("says nothing while Jev is up, or its health isn't known", () => {
    expect(jevNotice(true)).toBeNull();
    expect(jevNotice(null)).toBeNull();
  });

  it("says why documents wait and tools fail while Jev is down", () => {
    expect(jevNotice(false)).toBe(
      "Jev, the prompt-injection check, is down. New documents wait to be checked before they're searchable, and agents' code and data tools return errors until it's back.",
    );
  });
});
