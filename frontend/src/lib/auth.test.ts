import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { formErrors, nextPath, signInPath } from "@/lib/auth";

const origin = "https://aios.example";

describe("nextPath", () => {
  it("follows a path on this site, with its query and fragment", () => {
    expect(nextPath("/chats/3", origin)).toBe("/chats/3");
    expect(nextPath("/agents/1/runs/2?tab=steps#end", origin)).toBe("/agents/1/runs/2?tab=steps#end");
  });

  it("goes home for anything else", () => {
    for (const next of [
      null,
      "",
      "chats",
      "https://evil.example/chats",
      "//evil.example/chats",
      "/\\evil.example",
      "javascript:alert(1)",
      `${origin}/chats`,
    ]) {
      expect(nextPath(next, origin), String(next)).toBe("/");
    }
  });
});

describe("signInPath", () => {
  it("asks to come back to where the user was", () => {
    expect(signInPath("/chats/3", "?x=1", "#m")).toBe("/login?next=%2Fchats%2F3%3Fx%3D1%23m");
  });

  it("doesn't bother coming back home", () => {
    expect(signInPath("/", "", "")).toBe("/login");
  });
});

describe("formErrors", () => {
  it("puts a refusal's detail above the form", () => {
    const wrong = new ApiError(401, JSON.stringify({ detail: "Email or password is incorrect" }));
    expect(formErrors(wrong)).toEqual({ form: "Email or password is incorrect", fields: {} });
    const err = new ApiError(409, JSON.stringify({ detail: "An account with this email already exists" }));
    expect(formErrors(err)).toEqual({ form: "An account with this email already exists", fields: {} });
  });

  it("puts each invalid field's message next to it", () => {
    const err = new ApiError(
      422,
      JSON.stringify({
        detail: [
          { loc: ["body", "password"], msg: "Password must be at least 12 characters" },
          { loc: ["body", "email"], msg: "An email address must have an @-sign." },
        ],
      }),
    );
    expect(formErrors(err)).toEqual({
      fields: {
        password: "Password must be at least 12 characters",
        email: "An email address must have an @-sign.",
      },
    });
  });

  it("says what went wrong otherwise", () => {
    expect(formErrors(new ApiError(500, "<html>"))).toEqual({ form: "request failed (500)", fields: {} });
    expect(formErrors(new TypeError("Failed to fetch"))).toEqual({ form: "network error", fields: {} });
  });
});
