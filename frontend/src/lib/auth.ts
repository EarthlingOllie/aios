import { ApiError, describeError } from "@/lib/api";

/** The pages a signed-out visitor may see; every other path sends them to sign in. */
export const SIGNED_OUT_PATHS = ["/login", "/register"];

/**
 * Where to go after signing in, given the sign-in page's `next`: that path, when it's one on
 * this site (`origin`), else home. So a crafted link can't send someone elsewhere.
 */
export function nextPath(next: string | null, origin: string = window.location.origin): string {
  // Only a path: not a URL (even this site's), nor protocol-relative (`//host`, or `/\host`,
  // which browsers read as one).
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  try {
    const url = new URL(next, origin);
    if (url.origin !== origin) return "/";
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}

/** The sign-in page, asking to come back to the page at `pathname`, `search` and `hash`. */
export function signInPath(pathname: string, search: string, hash: string): string {
  const here = pathname + search + hash;
  return here === "/" ? "/login" : `/login?next=${encodeURIComponent(here)}`;
}

const TRY_AGAIN_LATER = "Too many attempts. Try again later.";

/**
 * What a sign-in or registration form shows for a failed request: a refusal (401, 409), being
 * throttled (429) or anything unexpected above the form, and each invalid field's message (422)
 * next to it.
 */
export function formErrors(err: unknown): { form?: string; fields: Record<string, string> } {
  if (err instanceof ApiError) {
    // Too many attempts (the API's rate limits, or a proxy's).
    if (err.status === 429) return { form: TRY_AGAIN_LATER, fields: {} };
    let detail: unknown;
    try {
      detail = JSON.parse(err.message).detail;
    } catch {
      // Not JSON (e.g. a proxy's error page): described below.
    }
    // The API's own words, e.g. "Email or password is incorrect" (`describeError` would call
    // a 401 an expired session).
    if (typeof detail === "string") return { form: detail, fields: {} };
    if (err.status === 422 && Array.isArray(detail)) {
      const fields: Record<string, string> = {};
      for (const { loc, msg } of detail as { loc: (string | number)[]; msg: string }[]) {
        fields[String(loc[loc.length - 1])] ??= msg;
      }
      return { fields };
    }
  }
  return { form: describeError(err), fields: {} };
}
