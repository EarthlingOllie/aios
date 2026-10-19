# Native email and password auth, and a dev sign-in picker

Status: **plan** (2026-10-19). Nothing here is built yet. The decisions marked **Open** need an
answer before work starts. They're collected in [Open questions](#open-questions).

## Goal

1. **Register** with name, email, password and confirm password.
2. **Sign in** with email and password.
3. Do both **without a third-party identity provider**: no OAuth, and no hosted or self-hosted
   identity service (Auth0, Clerk, Keycloak, Ory, SuperTokens, ...). Everything runs in the API we
   already have, against the platform database.
4. **Development only**: the sign-in page shows a dropdown of the users in the database. Picking
   one starts a session as that user with no password.

GitHub sign-in stays for now. Whether it stays for good is open (Q1).

## What we have today

- `aios/auth.py`: Authlib GitHub OAuth. `/login` redirects to GitHub. `/auth/callback` upserts
  the `User` by `github_id`, calls `data_store_for(user.id)` in the threadpool (which queues the
  user's data store to be provisioned), then sets `request.session["user_id"]`. `POST /logout`
  clears the session. `current_user` reads `user_id` from the session.
- **The session** is Starlette's `SessionMiddleware`: the whole session is in a cookie signed
  with itsdangerous (`aios_session`, 14 days, `SameSite=Lax`, `Secure` when `COOKIE_SECURE`).
  Nothing is stored on the server, so a session can't be revoked before it expires.
- **`User`** (`models.py`): `github_id` (unique, NOT NULL), `login` (NOT NULL, the GitHub
  handle), `name`, `email` (nullable, not unique), `avatar_url`. `login` is shown when there's no
  name (`user.name or user.login`) and is matched by chat @mentions (`filterMentionable`).
- **Routing**: the ALB (`infra/terraform/alb.tf`), nginx (`nginx.compose.conf`) and Vite's dev
  proxy send `/api/*`, `/login`, `/logout` and `/auth/*` to the API, and everything else to the
  SPA. **The SPA can't own a `/login` or `/auth/...` route.**
- **Frontend**: `App.tsx` calls `GET /api/me`. A 401 renders `<Login />` at whatever URL you're
  on. There's no form library. `components/ui` has `button`, `card`, `input`, `select` and
  `textarea`, but no `label` or `form`.
- **Tests**: backend tests build users with `User(github_id=..., login=...)` in about 15 places,
  and some sign in by writing a signed cookie by hand (`test_datastores_pool.py`). The
  Playwright tests run against an in-memory fake API (`e2e/fake-api.ts`), with no sign-in.
- **Nothing to send email with**: no SES, no SMTP, no email templates.
- **No CSRF protection** beyond `SameSite=Lax` and JSON request bodies.
- **No "is this development?" setting.** The nearest are `COOKIE_SECURE=false`,
  `SANDBOX_BACKEND=dev` and Terraform's `var.environment` (default `prod`, not passed to the
  app).

## Package research

The backend is FastAPI 0.142, **sync** SQLAlchemy 2.1 sessions (`DB` is a sync `Session`),
pydantic-settings and Redis. Dependencies are pinned exactly, and uv only takes releases more than
7 days old (`exclude-newer = "7 days"`). The frontend is React 19, React Router 8, shadcn/ui and
pnpm, which also waits 7 days for releases (`minimumReleaseAge`).

### Full auth frameworks: not recommended

| Package | Latest | Verdict |
| --- | --- | --- |
| **fastapi-users** | 15.0.5 (2026-03) | **In maintenance mode**: security fixes only, no new features. Its author says a successor is coming. It's built around **async** SQLAlchemy (`fastapi-users-db-sqlalchemy`, `AsyncSession`), and our whole app uses sync sessions. It brings its own `User` table shape (UUID id, `hashed_password`, `is_active`, `is_superuser`, `is_verified`), its own routers, and JWT or DB-token "strategies". Adopting it means a second session engine, a reshaped `users` table (our ids are integers referenced everywhere), and working around its routers to keep GitHub and the data store hook. Too much to take on for a library that's frozen. |
| **AuthX** | 1.7.1 (2026-06) | JWT-first, with tokens in headers or cookies. We already have a cookie session and don't want bearer tokens in the SPA. It handles tokens, not users or passwords. |
| **fastapi-login** | 1.10.3 (2024-12) | Flask-Login-style JWT manager. Not updated in almost two years. Same mismatch as AuthX. |
| **SuperTokens / Ory Kratos / Keycloak / Authentik / Zitadel** | n/a | Separate identity services. They're "3rd party integration", which is ruled out. |
| **Better Auth / Lucia** | n/a | TypeScript only. Better Auth would need a Node service. Lucia is now a guide, not a library. |

### Building blocks: recommended

The auth logic itself (register, sign in, sign out, the dev picker) is about 200 lines on top of
what `auth.py` already does. The parts that are easy to get wrong come from libraries:

| Need | Package | Notes |
| --- | --- | --- |
| Password hashing | **`pwdlib[argon2]`** 0.3.1 (2026-08), which pulls in **`argon2-cffi`** (25.1.0) | Argon2id, the current OWASP recommendation. pwdlib is what the FastAPI docs now use instead of passlib. `PasswordHash.recommended()` gives Argon2id with sane parameters. `verify_and_update()` re-hashes when parameters change, so we can raise the cost later. (Using `argon2-cffi` directly, with its `PasswordHasher`, works just as well and is one dependency fewer. Either is fine.) |
| ~~passlib~~ | 1.7.4 (2020) | **Avoid.** Unmaintained, and it breaks with `bcrypt` 4.1 and later. |
| Email validation | **`email-validator`** 2.3.0 | Makes pydantic's `EmailStr` work. It normalises the address. Turn off its DNS deliverability check (`check_deliverability=False`), or registration would depend on DNS. |
| Rate limiting | **`limits`** 5.8.0, with its Redis storage | Throttles sign-in and registration per IP and per email. `slowapi` (0.1.10) wraps it as decorators, but it's a thin, rarely released layer. Calling `limits` directly from the two endpoints (or a 20-line counter in Redis with `INCR` and `EXPIRE`) is simpler. We already run Redis. |
| Signed tokens (email verification, password reset) | **`itsdangerous`** (already a dependency) | `URLSafeTimedSerializer`. Only needed if we do verification or reset (Q4). |
| Server-side sessions (optional) | **`starsessions`** 2.2.1 (2024-10), with Redis | Only needed if we want sessions we can revoke (Q3). It's quiet but small. A `session_version` column (below) gets most of the benefit with no new package. |
| Password strength (optional) | `zxcvbn` (Python) / `@zxcvbn-ts/core` (frontend) | Optional. Following NIST 800-63B, a minimum length (and optionally a breached-password check) beats complexity rules. |

Frontend: **no new packages needed.** We have two small forms. Plain React state and the shadcn
`Input` and `Button` match how the rest of the app builds its forms. Add shadcn's `label`
component (which pulls in `@radix-ui/react-label`) or use a plain `<label>`.
`react-hook-form` (7.90) with `zod` (4.6) would be the choice if the forms grow, but they aren't
needed for this.

**Recommendation:** add `pwdlib[argon2]`, `email-validator` and `limits`. Write the endpoints in
`aios/auth.py` next to GitHub. Keep the cookie session we have.

## Design

### Data model (migration `0029_native_auth`)

`users`:

- `github_id`: drop NOT NULL. It's still unique, and Postgres allows many NULLs in a unique
  column.
- `password_hash TEXT NULL`: the Argon2id PHC string. NULL for GitHub-only users.
- `email`: make it unique, ignoring case. Either add a unique index on `lower(email)`, or store
  addresses normalised and add a plain unique constraint. Existing GitHub users may share an
  email or have none, so the migration has to check for duplicates first (see Q2).
- `login`: native users have no GitHub handle (see Q5). Either drop NOT NULL, or fill it with a
  handle we generate.
- `session_version INTEGER NOT NULL DEFAULT 0`: copied into the session at sign-in.
  `current_user` refuses a session whose value differs. Changing or resetting a password bumps
  it, which signs out every session for that user. This is the cheap way to revoke sessions
  without moving them to the server.
- `email_verified_at TIMESTAMPTZ NULL`: only if we verify emails (Q4).
- A check that every user can sign in somehow:
  `CHECK (github_id IS NOT NULL OR password_hash IS NOT NULL)`.

Tests build users as `User(github_id=..., login=...)`. They keep working, because GitHub users
are still valid. Add a `native_user` fixture next to `provisioned_user`.

### API

These go under `/api/auth/*`, which every proxy already routes to the API, so no ALB, nginx or
Vite change is needed. `/login`, `/logout` and `/auth/callback` keep doing what they do now.

| Endpoint | Body | Result |
| --- | --- | --- |
| `GET /api/auth/options` | none | `{ github: bool, password: bool, dev_users: bool }`, so the sign-in page knows what to show. `github` is true when `GITHUB_CLIENT_ID` is set. |
| `POST /api/auth/register` | `{ name, email, password, password_confirm }` | 201 and the user (the `/api/me` shape), signed in. 422 if the passwords differ or the password is too short. 409 if the email is taken (see Q6 on account enumeration). |
| `POST /api/auth/login` | `{ email, password }` | 200 and the user, signed in. 401 `"Email or password is incorrect"` for an unknown email and for a wrong password alike. |
| `POST /logout` | none | Unchanged. |
| `GET /api/me` | none | Unchanged, plus `email`. |

The sign-in steps shared by GitHub, registration, password sign-in and the dev picker become one
function, `start_session(request, user)`. It runs `data_store_for` in the threadpool, clears the
session, and sets `user_id` and `session_version`. The GitHub callback calls it too, so there's
one place where a session starts.

Details:

- **Normalise the email** before every lookup and insert: email-validator's normalised form,
  lower-cased.
- **Passwords**: at least 12 characters and at most 256 characters, because hashing very long
  inputs is a denial of service. No composition rules. `password_confirm` is checked on the
  server too, not only in the browser.
- **Timing**: when the email is unknown, verify the password against a fixed dummy hash anyway,
  so a response doesn't reveal whether the account exists by how fast it comes back.
- **Hash off the event loop**: Argon2 takes about 50 ms of CPU. The endpoints are sync `def`, so
  FastAPI already runs them in the threadpool.
- **Rate limits** (Redis, `limits`): sign-in at 10 per 15 minutes per email and 50 per 15 minutes
  per IP. Registration at 5 per hour per IP. Return 429 with `Retry-After`. Behind the ALB, the
  client IP is the last `X-Forwarded-For` hop the ALB added. Uvicorn needs
  `--proxy-headers --forwarded-allow-ips` for the ALB's range, or we parse the header ourselves.
- **CSRF**: sign-in is a JSON POST, and a cross-site HTML form can't send
  `Content-Type: application/json` without a CORS preflight, which we don't allow. Together with
  `SameSite=Lax`, that covers login CSRF. Keep it that way by rejecting a non-JSON body (FastAPI
  does by default for a pydantic body model), and add an `Origin` check against `PUBLIC_URL` on
  the auth POSTs as a second guard.
- **GitHub users with the same email** as a new native registration: see Q2.

### Dev sign-in picker

**Backend**: a new setting, `DEV_LOGIN_ENABLED` (default **false**). When it's on, the app adds a
small router, `aios/dev_auth.py`. When it's off, the router isn't added at all, so its routes are
404s rather than 403s:

- `GET /api/dev/users` lists `[{ id, name, login, email }]` for every user, ordered by name.
- `POST /api/dev/session` with `{ user_id }` calls `start_session` for that user and returns them.

Guards, so it can't be switched on in production by mistake:

- `create_app` refuses to start if `DEV_LOGIN_ENABLED` is set together with `COOKIE_SECURE=true`,
  or with a `PUBLIC_URL` whose host isn't `localhost` or `127.0.0.1`. Which guard is right
  depends on Q7.
- Terraform never sets it. `docker-compose.yml` sets `DEV_LOGIN_ENABLED: "true"` on the `api`
  service. `.env.example` documents it for `pnpm dev`.
- Every dev sign-in logs a warning naming the user.

**Frontend**: `Login` fetches `/api/auth/options`. When `dev_users` is true, it loads
`/api/dev/users` and shows a "Development: sign in as..." `Select` under the forms, with a visible
"dev only" badge. Picking a user posts to `/api/dev/session`, then runs the same `loadMe` that
`App` uses. The SPA build has no dev-only code path of its own: the backend decides. The
`docker compose` web image is a production build, so `import.meta.env.DEV` would hide the picker
there (see Q7).

`e2e/fake-api.ts` gains `/api/auth/options` (and the dev endpoints if a spec tests the picker),
so the Playwright tests keep working with no backend.

### Frontend

- `components/Login.tsx` becomes `AuthPage` with two modes, **Sign in** and **Create account**,
  switched by a link under the form.
  - Sign in: email and password.
  - Create account: name, email, password and confirm password. "Passwords don't match" shows on
    blur and on submit. Submit is disabled while a request is in flight.
  - "Sign in with GitHub" stays as a secondary button under an "or" divider when
    `options.github` is true.
  - Errors: a 401 or 409 shows above the form. A 422 shows next to its field. A 429 says to try
    again later.
- **Routes**: the page renders wherever the user is signed out, as `<Login />` does now. That
  keeps deep links working after sign-in. If we want shareable URLs for the two modes, use
  `/sign-in` and `/sign-up`. **Not** `/login` or `/register`: `/login` belongs to the API, and
  keeping auth paths clear of the proxied prefixes avoids that trap.
- `lib/api.ts`: add `authOptions()`, `register()`, `login()`, `devUsers()` and `devSignIn()`.
  The `User` type gains `email`.
- **Avatars**: native users have no `avatar_url`. `App.tsx` and `ChatAvatar` already handle null.
  Showing initials instead is optional polish.

### Infra and config

- `config.py`: `dev_login_enabled: bool = False`, `password_min_length: int = 12`, and the rate
  limit numbers.
- `.env.example` and the README: GitHub becomes optional, and `DEV_LOGIN_ENABLED` is documented.
  The README's Auth row in the stack table and the setup steps get updated.
- Terraform: no new secrets. `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` may be left empty.
  Today `secrets.tf` seeds them with `set-me`, so `options.github` must treat a placeholder like
  that as not configured, or the seed has to change.

## Build order

1. Migration and model changes. Update the test fixtures and add `native_user`.
2. `start_session` and `session_version`, with the GitHub callback moved onto it. Tests for
   revocation.
3. `register` and `login`, with hashing, normalisation, the timing guard and rate limits. Tests:
   happy paths, duplicate email, wrong password, unknown email (same response), mismatched
   confirmation, rate limit, the data store queued on registration.
4. The dev router and its start-up guards. Tests: absent when the setting is off, refuses to start
   with `COOKIE_SECURE`, signs in as the chosen user.
5. Frontend: `AuthPage`, the API client, the fake API and a Playwright spec for register, sign in
   and the dev picker.
6. README, `.env.example`, compose and Terraform.

## Open questions

These are the architectural ambiguities. Each one changes the schema, the API or the scope.

1. **Does GitHub sign-in stay?** The plan keeps both. If GitHub goes away, `github_id` and
   Authlib can be dropped later, but existing GitHub users then need a way in: a password reset by
   email (Q4), or a one-time "set a password" step while they're still signed in through GitHub.
2. **Account linking.** A GitHub user whose account has `alice@example.com` registers natively
   with the same email. Should we (a) refuse with "use GitHub to sign in", (b) link the accounts
   so both methods reach the same user, or (c) create a second user? Linking on email alone is
   only safe if we've verified the native email (Q4). Otherwise anyone could register with
   someone's address and take over their GitHub-created account. Existing GitHub users may also
   share emails or have none, which blocks a unique index until that's resolved.
3. **Session storage.** Keep the stateless signed cookie, with `session_version` for "sign out
   everywhere on password change"? Or move to server-side sessions in Redis (`starsessions`), so
   we can list and revoke single sessions? The plan keeps the cookie. Server-side sessions only
   pay off if we want a "your devices" page or an admin kill switch.
4. **Email verification and password reset.** Both need an email sender, and the app has none
   (it would need SES on AWS and Mailpit or console output locally). Without reset, a forgotten
   password means an admin edits the database. Without verification, email addresses are
   unproven, which rules out email-based linking (Q2) and makes open registration cheaper to
   abuse. Are they in scope for this piece of work or a follow-up?
5. **What replaces `login` for native users?** `login` is NOT NULL, is shown as a fallback name,
   and is matched by @mentions. The options are: (a) make it nullable and fall back to `name`;
   (b) generate a handle from the email's local part or the name, made unique; or (c) ask for a
   username at registration (the requested form has only name, email and passwords). Uniqueness
   matters if handles start being used as identifiers.
6. **Open registration.** Every registration provisions a Postgres database for the new user
   (`data_store_for`), which costs real resources. Should anyone who reaches the URL be able to
   sign up? Or should it be invite-only, an email-domain allowlist, or admin approval? This also
   decides whether "email already registered" may be revealed. With open sign-up it's hard to
   hide, because the 409 tells an attacker the account exists. Avoiding that needs the
   verification email flow (Q4): always answer "check your email".
7. **What counts as "development"?** The dev picker is gated on a backend setting. Should it also
   require a localhost `PUBLIC_URL`? That would rule out shared dev or staging deployments on AWS,
   which may be exactly where it's wanted. Or should it require `COOKIE_SECURE=false`, which
   would rule out HTTPS dev environments? And should a non-local staging environment ever have
   it? If so, it needs something stronger than a flag, such as an extra shared secret.
8. **Who is listed in the dev picker?** Every user in the database, as asked? Against a copy of
   production data, that lists real people's names and emails to anyone who reaches the page.
   That's another reason to tie the picker to environments with local data only.
9. **Account lockout or throttling only?** The plan rate-limits by email and IP but never locks
   an account, since a lockout lets anyone lock a victim out. Confirm that's acceptable, or say
   whether you want a lockout with an unlock email (which depends on Q4).
10. **Profile management.** Changing your name, email or password from the Settings page isn't
    in the request. Is it part of this work? Changing a password needs the current password and
    bumps `session_version`. Changing an email needs re-verification if Q4 is in scope.
