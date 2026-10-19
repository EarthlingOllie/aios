# Native email and password auth, profile editing, and a dev sign-in picker

Status: **plan** (2026-10-19, revised with the second round of decisions). Nothing here is built
yet. The remaining questions are at the end, in [Open questions](#open-questions).

## Goal

1. **Register** with first name, last name, email, password and confirm password.
2. **Sign in** with email and password.
3. **Replace GitHub sign-in.** No OAuth, and no hosted or self-hosted identity service (Auth0,
   Clerk, Keycloak, Ory, SuperTokens, ...). Everything runs in the API we already have, against
   the platform database.
4. **Edit your profile** on the Settings page: first name, last name, email and password.
5. **Development only**: the sign-in page shows a dropdown of the users in the database. Picking
   one starts a session as that user with no password.

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| 1 | Does GitHub sign-in stay? | **No, drop it.** The app hasn't shipped, so there are no existing users to migrate. |
| 2 | Linking GitHub and password accounts | Not applicable: there's no GitHub any more. |
| 3 | Email verification | **Out of scope for now.** No emails are sent. |
| 4 | What replaces `login` (the GitHub handle)? | **Drop `users.login`.** Users have a required **first name** and **last name**, and @mentions match on either. |
| 5 | Open registration | **Open for now.** Anyone who reaches the app can register. |
| 6 | What counts as "development"? | **An env var**, `DEV_LOGIN_ENABLED`. No other checks. |
| 7 | `session_version` | **Add it now.** A password change bumps it, which signs out the user's other sessions. |
| 8 | Profile editing | **In scope**: name, email and password. |
| 9 | Forgotten passwords | **Out of scope for now.** No reset flow and no operator tool. |

## What we have today

- `aios/auth.py`: Authlib GitHub OAuth. `/login` redirects to GitHub. `/auth/callback` upserts
  the `User` by `github_id`, calls `data_store_for(user.id)` in the threadpool (which queues the
  user's data store to be provisioned), then sets `request.session["user_id"]`. `POST /logout`
  clears the session. `current_user` reads `user_id` from the session.
- **The session** is Starlette's `SessionMiddleware`: the whole session is in a cookie signed
  with itsdangerous (`aios_session`, 14 days, `SameSite=Lax`, `Secure` when `COOKIE_SECURE`).
  Nothing is stored on the server, so a session can't be revoked before it expires.
- **`User`** (`models.py`): `github_id` (unique, NOT NULL), `login` (NOT NULL, the GitHub
  handle), `name`, `email` (nullable, not unique), `avatar_url`, and the run history settings.
- **Where `login` and `name` are used**: the display fallback `user.name or user.login` in the
  sidebar (`App.tsx`), chat messages (`api/chats.py`) and the handoff brief (`handoff.py`). The
  @mention search, which matches a member's name or login (`filterMentionable`). The chat member
  list, which shows `@login` (`pages/Chats.tsx`). And the API shapes: `/api/me`, chat authors and
  chat mentionables. A saved mention points at an id (`@[Name](user:42)`), so no saved data
  depends on `login`.
- **Routing**: the ALB (`infra/terraform/alb.tf`), nginx (`nginx.compose.conf`) and Vite's dev
  proxy send `/api/*`, `/login`, `/logout` and `/auth/*` to the API, and everything else to the
  SPA.
- **Frontend**: `App.tsx` calls `GET /api/me`. A 401 renders `<Login />` at whatever URL you're
  on. There's no form library. `components/ui` has `button`, `card`, `input`, `select` and
  `textarea`, but no `label` or `form`. The Settings page (`pages/Settings.tsx`) has the run
  history settings, backed by `GET` and `PUT /api/settings`.
- **Tests**: backend tests build users with `User(github_id=..., login=...)` in about 15 places,
  and some sign in by writing a signed cookie by hand (`test_datastores_pool.py`). The
  Playwright tests run against an in-memory fake API (`e2e/fake-api.ts`), with no sign-in.
- **No CSRF protection** beyond `SameSite=Lax` and JSON request bodies.

## Package research

The backend is FastAPI 0.142, **sync** SQLAlchemy 2.1 sessions (`DB` is a sync `Session`),
pydantic-settings and Redis. Dependencies are pinned exactly, and uv only takes releases more than
7 days old (`exclude-newer = "7 days"`). The frontend is React 19, React Router 8, shadcn/ui and
pnpm, which also waits 7 days for releases (`minimumReleaseAge`).

### Full auth frameworks: not recommended

| Package | Latest | Verdict |
| --- | --- | --- |
| **fastapi-users** | 15.0.5 (2026-03) | **In maintenance mode**: security fixes only, no new features. Its author says a successor is coming. It's built around **async** SQLAlchemy (`fastapi-users-db-sqlalchemy`, `AsyncSession`), and our whole app uses sync sessions. It brings its own `User` table shape (UUID id, `hashed_password`, `is_active`, `is_superuser`, `is_verified`), its own routers, and JWT or DB-token "strategies". Adopting it means a second session engine, a reshaped `users` table (our ids are integers referenced everywhere), and working around its routers to keep the data store hook. Too much to take on for a library that's frozen. |
| **AuthX** | 1.7.1 (2026-06) | JWT-first, with tokens in headers or cookies. We already have a cookie session and don't want bearer tokens in the SPA. It handles tokens, not users or passwords. |
| **fastapi-login** | 1.10.3 (2024-12) | Flask-Login-style JWT manager. Not updated in almost two years. Same mismatch as AuthX. |
| **SuperTokens / Ory Kratos / Keycloak / Authentik / Zitadel** | n/a | Separate identity services. They're "3rd party integration", which is ruled out. |
| **Better Auth / Lucia** | n/a | TypeScript only. Better Auth would need a Node service. Lucia is now a guide, not a library. |

### Building blocks: recommended

The auth logic itself (register, sign in, sign out, profile edits, the dev picker) is a few
hundred lines. The parts that are easy to get wrong come from libraries:

| Need | Package | Notes |
| --- | --- | --- |
| Password hashing | **`pwdlib[argon2]`** 0.3.1 (2026-08), which pulls in **`argon2-cffi`** (25.1.0) | Argon2id, the current OWASP recommendation. pwdlib is what the FastAPI docs now use instead of passlib. `PasswordHash.recommended()` gives Argon2id with sane parameters. `verify_and_update()` re-hashes when parameters change, so we can raise the cost later. (Using `argon2-cffi` directly, with its `PasswordHasher`, works just as well and is one dependency fewer. Either is fine.) |
| ~~passlib~~ | 1.7.4 (2020) | **Avoid.** Unmaintained, and it breaks with `bcrypt` 4.1 and later. |
| Email validation | **`email-validator`** 2.3.0 | Makes pydantic's `EmailStr` work. It normalises the address. Turn off its DNS deliverability check (`check_deliverability=False`), or registration would depend on DNS. |
| Rate limiting | **`limits`** 5.8.0, with its Redis storage | Throttles sign-in, registration and password checks. `slowapi` (0.1.10) wraps it as decorators, but it's a thin, rarely released layer. Calling `limits` directly from the endpoints (or a 20-line counter in Redis with `INCR` and `EXPIRE`) is simpler. We already run Redis. |
| Server-side sessions (not needed) | `starsessions` 2.2.1, with Redis | Only if we later want to list and revoke individual sessions. `session_version` covers "sign out my other sessions" without it. |

**Removed:** `authlib`, which was only used for GitHub OAuth. `httpx` is still used elsewhere.
`itsdangerous` stays, because `SessionMiddleware` signs the cookie with it.

Frontend: **no new packages needed.** The forms are small. Plain React state and the shadcn
`Input` and `Button` match how the rest of the app builds its forms. Add shadcn's `label`
component (which pulls in `@radix-ui/react-label`) or use a plain `<label>`.
`react-hook-form` (7.90) with `zod` (4.6) would be the choice if the forms grow, but they aren't
needed for this.

**Recommendation:** add `pwdlib[argon2]`, `email-validator` and `limits`, and remove `authlib`.
Keep the cookie session we have.

## Design

### Data model (migration `0029_native_auth`)

The app hasn't shipped, so the migration doesn't need to keep any existing users. Development
databases have a few GitHub users whose rows can't be filled in (they have no password). The
migration deletes them, which cascades to what they own, and the README says so. Resetting the
database works too.

`users`:

- Drop `github_id`, `login`, `name` and `avatar_url`.
- `first_name VARCHAR(100) NOT NULL` and `last_name VARCHAR(100) NOT NULL`. Both are trimmed,
  and neither may be empty.
- `email VARCHAR(320) NOT NULL UNIQUE`. It's stored normalised (email-validator's normalised
  form, lower-cased), so a plain unique constraint is enough.
- `password_hash TEXT NOT NULL`: the Argon2id PHC string.
- `session_version INTEGER NOT NULL DEFAULT 0`: copied into the session at sign-in.
  `current_user` refuses a session whose value differs from the user's.

A model property, `User.full_name` (`f"{first_name} {last_name}"`), is what every place that
shows a user uses. It replaces `user.name or user.login`.

Tests: about 15 places build `User(github_id=..., login=...)`. They change to a small helper,
`make_user(db, email=..., first_name=..., last_name=...)`, which sets a precomputed hash so
tests don't pay Argon2's cost each time. `provisioned_user` takes the same arguments.

### Sessions

`start_session(request, user)` is the one way a session starts: registration, sign-in and the dev
picker all call it. It runs `data_store_for` in the threadpool (so a new user's store is queued
at registration, as at GitHub sign-in today), clears the session, and sets `user_id` and
`session_version`.

`current_user` loads the user and answers 401 when the session's `session_version` differs from
the user's. A password change bumps the user's `session_version`, then calls `start_session`
again for the request making the change. The user stays signed in where they changed it, and
every other session (another browser, a stolen cookie) gets 401 on its next request.

### Auth API

GitHub's `/login` and `/auth/callback` are removed. Everything moves under `/api/auth/*`, so the
API's only routes outside `/api` are the health checks.

| Endpoint | Body | Result |
| --- | --- | --- |
| `GET /api/auth/options` | none | `{ dev_users: bool }`, so the sign-in page knows whether to show the dev picker. |
| `POST /api/auth/register` | `{ first_name, last_name, email, password, password_confirm }` | 201 and the user (the `/api/me` shape), signed in. 422 if a field is empty, the passwords differ or the password is too short. 409 if the email is taken. |
| `POST /api/auth/login` | `{ email, password }` | 200 and the user, signed in. 401 `"Email or password is incorrect"` for an unknown email and for a wrong password alike. |
| `POST /api/auth/logout` | none | 204. This replaces `POST /logout`. |
| `GET /api/me` | none | `{ id, first_name, last_name, email }`. |

Details:

- **Normalise the email** the same way before every lookup and insert.
- **Passwords**: at least 12 characters and at most 256 characters, because hashing very long
  inputs is a denial of service. No composition rules (NIST 800-63B). The confirmation is checked
  on the server too, not only in the browser.
- **Timing**: when the email is unknown, verify the password against a fixed dummy hash anyway,
  so a response doesn't reveal whether the account exists by how fast it comes back.
  (Registration and email changes still reveal it through the 409. That's accepted while
  registration is open and there's no email to send.)
- **Hash off the event loop**: Argon2 takes about 50 ms of CPU. The endpoints are sync `def`, so
  FastAPI already runs them in the threadpool.
- **Rate limits** (Redis, `limits`): sign-in at 10 per 15 minutes per email and 50 per 15 minutes
  per IP. Registration at 5 per hour per IP. Checks of the current password (profile edits) at 10
  per 15 minutes per user. Return 429 with `Retry-After`. No account lockout, since a lockout lets
  anyone lock a victim out. Behind the ALB, the client IP is the last `X-Forwarded-For` hop the
  ALB added. Uvicorn needs `--proxy-headers --forwarded-allow-ips` for the ALB's range, or we
  parse the header ourselves.
- **CSRF**: the auth and profile endpoints take JSON, and a cross-site HTML form can't send
  `Content-Type: application/json` without a CORS preflight, which we don't allow. Together with
  `SameSite=Lax`, that covers login CSRF. As a second guard, check `Origin` against `PUBLIC_URL`
  on the auth and profile POSTs and PATCHes.

### Profile API

| Endpoint | Body | Result |
| --- | --- | --- |
| `PATCH /api/me` | any of `{ first_name, last_name }` | 200 and the user. No password needed. |
| `PUT /api/me/email` | `{ email, current_password }` | 200 and the user. 401 if the password is wrong. 409 if the email is taken. 422 if it's invalid. |
| `PUT /api/me/password` | `{ current_password, new_password, new_password_confirm }` | 204. 401 if the current password is wrong. 422 if the new passwords differ or the new one is too short. Bumps `session_version` and re-issues this request's session, as above. |

- Changing the email asks for the current password, because the email is what signs you in.
  Someone with a moment at an unlocked browser could otherwise move the account to an address
  they control. With no verification email, the new address takes effect immediately.
- Changing the email doesn't bump `session_version`. Only a password change signs out other
  sessions.
- The run history settings stay where they are, on `/api/settings`. The profile is a separate
  resource because it has different rules: unique email, password checks.

### Names in chats and @mentions

- **Display**: chat authors, the member list, the sidebar and the handoff brief show
  `full_name`. The `name` field in chat API shapes (`MessageAuthorOut`, `ChatMemberOut`, `MentionableOut`) stays
  as the display name, so agents and workflows keep using it unchanged. Users' entries get the
  full name, and the `login` and `avatar_url` fields are removed.
- **Mention search**: a user entry in the mentionables gains `first_name` and `last_name`.
  `filterMentionable` matches a user when the query appears in their first name or their last
  name, ignoring case. `@ali` finds Alice Smith, and so does `@smi`. Agents and workflows keep
  matching on `name`. The query can't contain a space (`mentionQuery` stops at whitespace), so
  `@alice smith` isn't a query. Matching either part covers it.
- **Mention tokens** use the full name (`@[Alice Smith](user:42)`). Since a mention resolves by id,
  a renamed user's old mentions still point at them. The chip shows their current name from the
  message's structured `mentions`.
- **The member list** shows the full name only. The `@login` line goes away.

### Routing

With `/login`, `/logout` and `/auth/*` gone from the API, drop them from the ALB's `api_paths`,
from `nginx.compose.conf`'s location regex and from Vite's proxy. Then the SPA can own clean URLs:
**`/login`** and **`/register`**.

Deploy order matters on AWS: the ALB rule change and the new API image must go out together, or
`/login` briefly reaches an API that no longer serves it. That's harmless before launch.

### Dev sign-in picker

**Backend**: a new setting, `DEV_LOGIN_ENABLED` (default **false**). When it's on, `create_app`
adds a small router, `aios/dev_auth.py`. When it's off, the router isn't added at all, so its
routes are 404s rather than 403s:

- `GET /api/dev/users` lists `[{ id, first_name, last_name, email }]` for every user, ordered by
  last name, then first name.
- `POST /api/dev/session` with `{ user_id }` calls `start_session` for that user and returns them.

As decided, the env var is the only gate. To keep it from being switched on by mistake:

- Terraform never sets it. `docker-compose.yml` sets `DEV_LOGIN_ENABLED: "true"` on the `api`
  service. `.env.example` documents it for `pnpm dev`.
- The API logs a warning at start-up when it's on, and every dev sign-in logs which user was
  chosen.

**Frontend**: the sign-in page fetches `/api/auth/options`. When `dev_users` is true, it loads
`/api/dev/users` and shows a "Development: sign in as..." `Select` under the form, with a visible
"dev only" badge. Each option reads "Alice Smith (alice@example.com)". Picking a user posts to
`/api/dev/session`, then loads `/api/me` as `App` does now. The SPA has no build-time dev flag
for this: the backend decides. (The compose web image is a production build, so
`import.meta.env.DEV` would hide the picker there.)

`e2e/fake-api.ts` gains the auth, profile and dev endpoints, so the Playwright tests keep working
with no backend.

### Frontend

- **Sign-in and registration**: `components/Login.tsx` becomes two pages, **`/login`** (email and
  password) and **`/register`** (first name, last name, email, password and confirm password),
  linked to each other.
  - Register: "Passwords don't match" shows on blur and on submit. Submit is disabled while a
    request is in flight.
  - Errors: a 401 or 409 shows above the form. A 422 shows next to its field. A 429 says to try
    again later.
  - A signed-out visit to any other URL redirects to `/login?next=<path>`. After sign-in, the user
    goes back to `next` if it's a same-origin path, and otherwise to `/`. Today `<Login />`
    renders in place at any URL. The redirect keeps deep links working and gives the two forms
    shareable URLs.
  - Visiting `/login` or `/register` while signed in redirects to `/`.
  - A 401 from any request while signed in (for example after a password change somewhere else)
    sends the user to `/login?next=<path>`, rather than showing an error.
- **Settings page**: a new **Profile** section above the run history settings, with three forms,
  each saved on its own:
  - **Name**: first and last name, with Save.
  - **Email**: the new email and the current password.
  - **Password**: the current password, the new one and its confirmation. On success it says
    "Password changed. You've been signed out everywhere else."
  - After a save, the sidebar and the current user context show the new name and email without a
    reload. `App` gets an `onUserChange` callback, or a `refreshMe()` in the current user context.
- `lib/api.ts`: add `authOptions()`, `register()`, `login()`, `updateProfile()`,
  `changeEmail()`, `changePassword()`, `devUsers()` and `devSignIn()`. `logout()` points at
  `/api/auth/logout`. The `User` type becomes `{ id, first_name, last_name, email }`, with a
  `fullName(user)` helper.
- Remove `login` and `avatar_url` from the UI: the sidebar, chat authors, the member list and the
  mention search. Avatars fall back to what's shown today when there's no picture.

### Infra and config

- `config.py`: remove `github_client_id`, `github_client_secret` and `oauth_redirect_uri`. Add
  `dev_login_enabled: bool = False`, `password_min_length: int = 12`, and the rate limit numbers.
- Terraform: remove `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` from `secrets.tf` and `ecs.tf`,
  and the GitHub callback note from `outputs.tf`. Trim `api_paths` in `alb.tf`.
- `.env.example` and the README: remove the GitHub OAuth app setup, document
  `DEV_LOGIN_ENABLED`, and update the Auth row in the stack table and the architecture diagram's
  routed paths.

## Build order

1. Migration and model changes, with `full_name`, the `make_user` test helper and the fixtures
   updated.
2. Sessions: `start_session`, and `session_version` checked in `current_user`.
3. `register`, `login` and `logout`, with hashing, normalisation, the timing guard and rate
   limits. Remove GitHub and Authlib. Tests: happy paths, duplicate email (any case), wrong
   password, unknown email (same response), mismatched confirmation, empty names, rate limit, the
   data store queued at registration.
4. Profile: `PATCH /api/me`, `PUT /api/me/email` and `PUT /api/me/password`. Tests: wrong current
   password, email taken, a password change signs out another session but keeps this one, rate
   limit.
5. The dev router. Tests: absent when the setting is off, signs in as the chosen user when it's
   on.
6. Names in chats and the handoff: `full_name` in the API shapes, `login` and `avatar_url`
   removed, first and last names in the mentionables.
7. Frontend: `/login`, `/register`, the redirect and 401 handling, the Settings profile section,
   mention matching on first and last name, the dev picker, the API client and the fake API.
   Playwright specs for registering, signing in, editing the profile and the dev picker.
   Unit tests for `filterMentionable`.
8. Routing (ALB, nginx, Vite), Terraform, README and `.env.example`.

## Open questions

All the architectural questions are decided. Two small product calls remain. The plan assumes the
first option for each:

1. **Name order**: show names as "First Last" everywhere, and sort the dev picker by last name?
   (The alternative is sorting by first name.)
2. **Deleting your account** isn't in the request, and it isn't planned. It would also have to
   delete the user's data store (`DataStoreStatus.deleting` exists, but nothing uses it yet).
   Leave it for later?
