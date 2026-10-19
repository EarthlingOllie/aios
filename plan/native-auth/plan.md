# Native email and password auth, and a dev sign-in picker

Status: **plan** (2026-10-19, revised with the first round of decisions). Nothing here is built
yet. The questions still open are at the end, in [Open questions](#open-questions).

## Goal

1. **Register** with name, email, password and confirm password.
2. **Sign in** with email and password.
3. **Replace GitHub sign-in.** No OAuth, and no hosted or self-hosted identity service (Auth0,
   Clerk, Keycloak, Ory, SuperTokens, ...). Everything runs in the API we already have, against
   the platform database.
4. **Development only**: the sign-in page shows a dropdown of the users in the database. Picking
   one starts a session as that user with no password.

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| 1 | Does GitHub sign-in stay? | **No, drop it.** The app hasn't shipped, so there are no existing users to migrate. |
| 2 | Linking GitHub and password accounts | Not applicable: there's no GitHub any more. |
| 3 | Email verification and password reset (no email sender exists) | **Out of scope for now.** No emails are sent. A forgotten password is fixed by hand for now. |
| 4 | What replaces `login` (the GitHub handle)? | **Proposed: drop it, and use `name`** (see [The `login` field](#the-login-field)). Waiting for confirmation. |
| 5 | Open registration | **Open for now.** Anyone who reaches the app can register. |
| 6 | What counts as "development"? | **An env var**, `DEV_LOGIN_ENABLED`. No other checks. |

## What we have today

- `aios/auth.py`: Authlib GitHub OAuth. `/login` redirects to GitHub. `/auth/callback` upserts
  the `User` by `github_id`, calls `data_store_for(user.id)` in the threadpool (which queues the
  user's data store to be provisioned), then sets `request.session["user_id"]`. `POST /logout`
  clears the session. `current_user` reads `user_id` from the session.
- **The session** is Starlette's `SessionMiddleware`: the whole session is in a cookie signed
  with itsdangerous (`aios_session`, 14 days, `SameSite=Lax`, `Secure` when `COOKIE_SECURE`).
  Nothing is stored on the server, so a session can't be revoked before it expires.
- **`User`** (`models.py`): `github_id` (unique, NOT NULL), `login` (NOT NULL, the GitHub
  handle), `name`, `email` (nullable, not unique), `avatar_url`.
- **Routing**: the ALB (`infra/terraform/alb.tf`), nginx (`nginx.compose.conf`) and Vite's dev
  proxy send `/api/*`, `/login`, `/logout` and `/auth/*` to the API, and everything else to the
  SPA.
- **Frontend**: `App.tsx` calls `GET /api/me`. A 401 renders `<Login />` at whatever URL you're
  on. There's no form library. `components/ui` has `button`, `card`, `input`, `select` and
  `textarea`, but no `label` or `form`.
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

The auth logic itself (register, sign in, sign out, the dev picker) is about 200 lines. The parts
that are easy to get wrong come from libraries:

| Need | Package | Notes |
| --- | --- | --- |
| Password hashing | **`pwdlib[argon2]`** 0.3.1 (2026-08), which pulls in **`argon2-cffi`** (25.1.0) | Argon2id, the current OWASP recommendation. pwdlib is what the FastAPI docs now use instead of passlib. `PasswordHash.recommended()` gives Argon2id with sane parameters. `verify_and_update()` re-hashes when parameters change, so we can raise the cost later. (Using `argon2-cffi` directly, with its `PasswordHasher`, works just as well and is one dependency fewer. Either is fine.) |
| ~~passlib~~ | 1.7.4 (2020) | **Avoid.** Unmaintained, and it breaks with `bcrypt` 4.1 and later. |
| Email validation | **`email-validator`** 2.3.0 | Makes pydantic's `EmailStr` work. It normalises the address. Turn off its DNS deliverability check (`check_deliverability=False`), or registration would depend on DNS. |
| Rate limiting | **`limits`** 5.8.0, with its Redis storage | Throttles sign-in and registration per IP and per email. `slowapi` (0.1.10) wraps it as decorators, but it's a thin, rarely released layer. Calling `limits` directly from the two endpoints (or a 20-line counter in Redis with `INCR` and `EXPIRE`) is simpler. We already run Redis. |
| Server-side sessions (not needed now) | `starsessions` 2.2.1, with Redis | Only if we later want to list and revoke individual sessions. The `session_version` column (below) covers "sign out everywhere" without it. |

**Removed:** `authlib`, which was only used for GitHub OAuth. `httpx` is still used elsewhere.
`itsdangerous` stays, because `SessionMiddleware` signs the cookie with it.

Frontend: **no new packages needed.** We have two small forms. Plain React state and the shadcn
`Input` and `Button` match how the rest of the app builds its forms. Add shadcn's `label`
component (which pulls in `@radix-ui/react-label`) or use a plain `<label>`.
`react-hook-form` (7.90) with `zod` (4.6) would be the choice if the forms grow, but they aren't
needed for this.

**Recommendation:** add `pwdlib[argon2]`, `email-validator` and `limits`, and remove `authlib`.
Keep the cookie session we have.

## The `login` field

`users.login` holds the user's GitHub handle (for example `octocat`), copied from GitHub at
sign-in. It is NOT NULL, and it's used in four places:

1. **Display fallback**: `user.name or user.login` in the sidebar (`App.tsx`), in chat messages
   (`api/chats.py`) and in the handoff brief (`handoff.py`). GitHub names are optional, so the
   handle was the fallback.
2. **@mention search**: typing `@oct` in a chat matches members whose name *or* login contains
   `oct` (`filterMentionable`). The mention itself is stored by id (`@[Name](user:42)`), not by
   login, so nothing persisted depends on it.
3. **Member list**: the chat's member list shows `@login` next to the name when they differ
   (`pages/Chats.tsx`).
4. **API shapes**: `/api/me`, chat authors and chat mentionables all return `login`.

With GitHub gone, nothing fills it, and the registration form only asks for name, email and
password. The options were:

- **(a) Drop it, and make `name` required.** Registration already asks for a name, so there's
  always one to show. Mentions search by name only. *Proposed.*
- (b) Generate a handle (`alice` from `alice@example.com`, with a number added when it's taken).
  This keeps `@handle` in the UI, but it's a made-up identifier nobody chose.
- (c) Add a username field to the registration form. That's a change to the requested form, and
  it's only worth it if handles become something people type or share.
- (d) Fill it with the email. Not good: it would show everyone's email to the other members of
  their chats.

The rest of this plan assumes **(a)**. `avatar_url` goes the same way, since only GitHub
supplied it. The UI already handles a missing avatar, and showing initials instead is optional
polish.

## Design

### Data model (migration `0029_native_auth`)

The app hasn't shipped, so the migration doesn't need to keep any existing users. It can delete
any rows left in development databases, or the README can say to reset the database.

`users`:

- Drop `github_id`, `login` and `avatar_url`.
- `name`: becomes NOT NULL.
- `email`: becomes NOT NULL and unique. Store it normalised (email-validator's normalised form,
  lower-cased), so a plain unique constraint is enough.
- `password_hash TEXT NOT NULL`: the Argon2id PHC string.
- `session_version INTEGER NOT NULL DEFAULT 0`: copied into the session at sign-in.
  `current_user` refuses a session whose value differs. Nothing bumps it yet, because there's no
  password change or reset. It's here so those can sign out every session later without a
  migration. If you'd rather not add it until then, it can wait.

Tests: about 15 places build `User(github_id=..., login=...)`. They change to a small helper,
`make_user(db, email=..., name=...)`, which sets a precomputed hash so tests don't pay Argon2's
cost each time. `provisioned_user` takes `email` and `name` instead of `github_id` and `login`.

### API

GitHub's `/login` and `/auth/callback` are removed. Everything moves under `/api/auth/*`, so the
API's only routes outside `/api` are the health checks.

| Endpoint | Body | Result |
| --- | --- | --- |
| `GET /api/auth/options` | none | `{ dev_users: bool }`, so the sign-in page knows whether to show the dev picker. |
| `POST /api/auth/register` | `{ name, email, password, password_confirm }` | 201 and the user (the `/api/me` shape), signed in. 422 if the passwords differ or the password is too short. 409 if the email is taken. |
| `POST /api/auth/login` | `{ email, password }` | 200 and the user, signed in. 401 `"Email or password is incorrect"` for an unknown email and for a wrong password alike. |
| `POST /api/auth/logout` | none | 204. This replaces `POST /logout`. |
| `GET /api/me` | none | `{ id, name, email }`. |

Registration, password sign-in and the dev picker all start a session through one function,
`start_session(request, user)`. It runs `data_store_for` in the threadpool (so a new user's store
is queued at registration, as at GitHub sign-in today), clears the session, and sets `user_id`
and `session_version`.

Details:

- **Normalise the email** the same way before every lookup and insert.
- **Passwords**: at least 12 characters and at most 256 characters, because hashing very long
  inputs is a denial of service. No composition rules (NIST 800-63B). `password_confirm` is
  checked on the server too, not only in the browser.
- **Timing**: when the email is unknown, verify the password against a fixed dummy hash anyway,
  so a response doesn't reveal whether the account exists by how fast it comes back. (Registration
  still reveals it through the 409. That's accepted while registration is open and there's no
  email to send.)
- **Hash off the event loop**: Argon2 takes about 50 ms of CPU. The endpoints are sync `def`, so
  FastAPI already runs them in the threadpool.
- **Rate limits** (Redis, `limits`): sign-in at 10 per 15 minutes per email and 50 per 15 minutes
  per IP. Registration at 5 per hour per IP. Return 429 with `Retry-After`. No account lockout,
  since a lockout lets anyone lock a victim out. Behind the ALB, the client IP is the last
  `X-Forwarded-For` hop the ALB added. Uvicorn needs `--proxy-headers --forwarded-allow-ips` for
  the ALB's range, or we parse the header ourselves.
- **CSRF**: the auth endpoints take JSON, and a cross-site HTML form can't send
  `Content-Type: application/json` without a CORS preflight, which we don't allow. Together with
  `SameSite=Lax`, that covers login CSRF. As a second guard, check `Origin` against `PUBLIC_URL`
  on the auth POSTs.

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

- `GET /api/dev/users` lists `[{ id, name, email }]` for every user, ordered by name.
- `POST /api/dev/session` with `{ user_id }` calls `start_session` for that user and returns them.

As decided, the env var is the only gate. To keep it from being switched on by mistake:

- Terraform never sets it. `docker-compose.yml` sets `DEV_LOGIN_ENABLED: "true"` on the `api`
  service. `.env.example` documents it for `pnpm dev`.
- The API logs a warning at start-up when it's on, and every dev sign-in logs which user was
  chosen.

**Frontend**: the sign-in page fetches `/api/auth/options`. When `dev_users` is true, it loads
`/api/dev/users` and shows a "Development: sign in as..." `Select` under the form, with a visible
"dev only" badge. Picking a user posts to `/api/dev/session`, then loads `/api/me` as `App` does
now. The SPA has no build-time dev flag for this: the backend decides. (The compose web image is
a production build, so `import.meta.env.DEV` would hide the picker there.)

`e2e/fake-api.ts` gains the auth endpoints and the dev endpoints, so the Playwright tests keep
working with no backend.

### Frontend

- `components/Login.tsx` becomes two pages, **`/login`** (email and password) and
  **`/register`** (name, email, password and confirm password), linked to each other.
  - Register: "Passwords don't match" shows on blur and on submit. Submit is disabled while a
    request is in flight.
  - Errors: a 401 or 409 shows above the form. A 422 shows next to its field. A 429 says to try
    again later.
  - A signed-out visit to any other URL redirects to `/login?next=<path>`. After sign-in, the user
    goes back to `next` if it's a same-origin path, and otherwise to `/`. Today `<Login />`
    renders in place at any URL. The redirect keeps deep links working and gives the two forms
    shareable URLs.
  - Visiting `/login` or `/register` while signed in redirects to `/`.
- `lib/api.ts`: add `authOptions()`, `register()`, `login()`, `devUsers()` and `devSignIn()`.
  `logout()` points at `/api/auth/logout`. The `User` type becomes `{ id, name, email }`.
- Remove `login` and `avatar_url` from the UI: the sidebar, chat authors, the chat member list
  and the mention search. Avatars fall back to what's shown today when there's no picture.

### Infra and config

- `config.py`: remove `github_client_id`, `github_client_secret` and `oauth_redirect_uri`. Add
  `dev_login_enabled: bool = False`, `password_min_length: int = 12`, and the rate limit numbers.
- Terraform: remove `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` from `secrets.tf` and `ecs.tf`,
  and the GitHub callback note from `outputs.tf`. Trim `api_paths` in `alb.tf`.
- `.env.example` and the README: remove the GitHub OAuth app setup, document
  `DEV_LOGIN_ENABLED`, and update the Auth row in the stack table and the architecture diagram's
  routed paths.

## Build order

1. Migration and model changes, with the `make_user` test helper and the fixtures updated.
2. `start_session`, `register`, `login` and `logout`, with hashing, normalisation, the timing
   guard and rate limits. Remove GitHub and Authlib. Tests: happy paths, duplicate email,
   wrong password, unknown email (same response), mismatched confirmation, rate limit, the data
   store queued at registration.
3. The dev router. Tests: absent when the setting is off, signs in as the chosen user when it's
   on.
4. Drop `login` and `avatar_url` from the API shapes (`/api/me`, chats, handoff).
5. Frontend: `/login`, `/register`, the redirect, the dev picker, the API client and the fake
   API. A Playwright spec for registering, signing in and the dev picker.
6. Routing (ALB, nginx, Vite), Terraform, README and `.env.example`.

## Open questions

1. **Confirm the `login` proposal** (drop it and use `name`), or pick one of the other options in
   [The `login` field](#the-login-field).
2. **`session_version`**: add it now (the cost is one column), or wait until password change or
   reset exists?
3. **Profile management**: changing your name, email or password from the Settings page isn't in
   the request. Is it part of this work or a follow-up?
4. **Recovering a forgotten password** without email: until an email sender exists, an operator
   resets it by hand. Do you want a small CLI for that (`python -m aios.users set-password
   <email>`), or is editing the database acceptable for now?
