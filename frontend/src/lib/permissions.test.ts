import { describe, expect, it } from "vitest";

import { can, refusal, roleLabel } from "@/lib/permissions";

describe("can", () => {
  it("lets every role read, chat and start runs", () => {
    for (const role of ["admin", "editor", "participant"] as const) {
      expect(can(role, "read")).toBe(true);
      expect(can(role, "post")).toBe(true);
    }
  });

  it("lets editors and admins, not participants, build and change documents", () => {
    for (const action of ["build", "documents"] as const) {
      expect(can("admin", action)).toBe(true);
      expect(can("editor", action)).toBe(true);
      expect(can("participant", action)).toBe(false);
    }
  });

  it("keeps running the organisation to admins", () => {
    expect(can("admin", "run_organisation")).toBe(true);
    expect(can("editor", "run_organisation")).toBe(false);
    expect(can("participant", "run_organisation")).toBe(false);
  });

  it("treats an unknown role as able to do only what everyone can", () => {
    expect(can(undefined, "read")).toBe(true);
    expect(can(undefined, "build")).toBe(false);
  });
});

describe("refusal", () => {
  it("names the action and whom to ask", () => {
    expect(refusal("participant", "build")).toBe(
      "Participants can't create, edit, archive or unarchive agents, skills, workflows and schedules. Ask an editor or an admin.",
    );
    expect(refusal("editor", "run_organisation")).toBe(
      "Editors can't change the organisation's roles, invitations, name or settings, or delete it. Ask an admin.",
    );
  });

  it("is null for what the role may do", () => {
    expect(refusal("editor", "build")).toBeNull();
  });
});

describe("roleLabel", () => {
  it("capitalises the role", () => {
    expect(roleLabel("participant")).toBe("Participant");
  });
});
