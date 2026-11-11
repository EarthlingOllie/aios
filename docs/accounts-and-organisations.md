# Accounts and organisations

aios has its own accounts: email and password, with no third-party sign-in. A person is a
member of any number of organisations, each of which owns everything its members make. See also
[Stores](stores.md), for the organisation's own database, and the glossary,
[`CONTEXT.md`](../CONTEXT.md).

## Accounts

People register at `/register` (first name, last name, email, and a password typed twice) and
sign in at `/login` with their email and password. Both pages are the SPA's; they call the API
under `/api/auth` (`register`, `login`, `logout`, and `options`, which says whether the sign-in
page offers anything more), and `GET /api/me` answers who's signed in. There's no third-party
sign-in, and no emails are sent (no verification, no password reset).

- **Passwords** are 12 to 256 characters, with no rules about what's in them, and stored as
  Argon2id hashes (`aios.passwords`). A wrong password and an unknown email get the same answer
  ("Email or password is incorrect"), and take as long: an unknown email is checked against a
  dummy hash.
- **Emails** are trimmed, normalised and lower-cased before they're stored or looked up, so one
  address in any case is one account. Registering a taken one answers 409.
- **Names** are trimmed and must not be empty; the app shows a user as "First Last".
- **Sessions** are the signed cookie (`aios_session`, 14 days). Registering signs the new user
  in, and makes nothing: they're in no organisation (or only the one an invitation link is
  for), and land on the welcome page. Signing in makes nothing either
  (`aios.auth.start_session` starts every session).
  Each session holds the user's `session_version` from when it started; one that no longer
  matches the user's is signed out (401).
- A signed-out visit to any page sends you to `/login?next=<page>`, and back there afterwards
  (only ever to a page on aios itself). So does a request that finds the session has ended
  while you're using the app (401 "Not signed in"), e.g. after a password change elsewhere.
- **Profile** (Settings): `PATCH /api/me` changes your name. `PUT /api/me/email` changes your
  email, straight away (nothing is sent to it), given your current password; 409 if it's taken.
  `PUT /api/me/password` changes your password, given the current one and the new one twice,
  and bumps your `session_version`, which signs out every other session; the one that made the
  change starts again, so it's kept. A wrong current password answers 401.
- **Dev sign-in picker**: with `DEV_LOGIN_ENABLED=true`, `/login` also offers a "sign in as"
  dropdown of every user, which signs in as them with no password (`aios.dev_auth`, under
  `/api/dev`). Docker Compose turns it on for its `api`, and `backend/.env.example` does for
  `pnpm dev` against a local API; it's off by default, and Terraform never sets it. When it's
  off, `/api/dev/*` doesn't exist (404). The API logs a warning at start-up when it's on, and
  names the user at each dev sign-in.
- **Rate limits** (`aios.ratelimits`, the `limits` package, counted in Redis): signing in, 10
  attempts per 15 minutes per email and 50 per IP; registering, 5 per hour per IP; checking a
  signed-in user's current password (the email and password forms), 10 per 15 minutes per
  user. Past one the API answers 429 with `Retry-After` (and the page says to try again later);
  nobody is locked out. The numbers are settings (`LOGIN_RATE_LIMIT_PER_EMAIL`,
  `LOGIN_RATE_LIMIT_PER_IP`, `REGISTER_RATE_LIMIT_PER_IP`,
  `CURRENT_PASSWORD_RATE_LIMIT_PER_USER`). The IP is the one the load balancer (or Compose's
  nginx) appended to `X-Forwarded-For`, given `TRUSTED_PROXY_COUNT=1` (Terraform and Compose set
  it); with 0 (the default, for `pnpm dev` against a local API) it's the connection's.
- **Cross-site requests**: the auth and profile endpoints only take JSON bodies, and refuse
  (403) a POST, PUT, PATCH or DELETE whose `Origin` isn't `PUBLIC_URL`'s.

**Upgrading a database from GitHub sign-in.** Migration `0052` deletes the users who signed in
with GitHub (they have no password), and, as nobody is then left in any organisation, every
organisation with everything in it. Their stores go `deleting`, for the provisioning worker's
sweep to drop their databases, roles and files (see [Stores](stores.md#data-stores)). On a development database it's as simple to start again:
`docker compose down -v` (which also empties RustFS), or drop and recreate the `aios` database,
then run the setup steps again (`aios.db_roles`, `alembic upgrade head`: see
[Local development](development.md#local-development)).

## Organisations

A person is in any number of **organisations** (`aios.organisations`), each the group that owns
what its members make, or in none. Signing up makes nothing: a person in no organisation lands on
the **welcome page** (`/welcome`), which creates one (`POST /api/organisations`, a name, which
another organisation may have too: the slug tells them apart) with them as its creator and only member, its store queued
to be provisioned at once, and opens it; it also explains that an invitation link is the way into
a colleague's. Memberships are a table, one row per person and organisation (revision 0058
dropped the one-per-person index), and the request's organisation is resolved in one place
(`aios.auth.current_organisation`, the `CurrentOrganisation` dependency). Before many
organisations, registering made one named after the user, and the upgrade did the same for
every existing user and stamped everything they own (skills, agents, workflows, schedules,
chats) with their organisation and with them as `created_by` (revisions 0029 and 0030); the
per-user ownership columns went in the same release (0048, which says why, as 0015 did).
Revision 0059 dropped the flag that marked those organisations, and the uniqueness of names.

### Organisations in links

Every organisation has a unique **slug**, made from its name when it's created (lower-case ASCII
words joined by `-`, `organisation` when nothing's left; `aios.organisations.free_slug`),
suffixed `-2`, `-3`, ... when another organisation has it, and never changed by a rename, so a
shared link keeps working. Revision 0055 gave existing organisations theirs, oldest first. Links
in the browser use the slug and the API uses the id:

- **API.** Every route that resolves the request's organisation (its dependencies reach
  `current_organisation`, as `CurrentOrganisation` and `CurrentDataStore` do) is mounted under
  the organisation prefix, `/api/o/{organisation_id}/...`, and only there (`aios.api.scoping`,
  from the router, so a new route moves as soon as it takes the organisation; no unprefixed
  form is kept). `current_organisation` reads the id from the path and checks the user is a
  member: a 404, "Organisation not found", alike for an organisation they aren't in and one that
  doesn't exist. Only what's the person's own stays unscoped, and only what's listed in
  `aios.api.scoping.UNSCOPED`: signing in (and the development picker), their profile
  (`/api/me`), creating an organisation, opening and accepting invitations, their
  notifications, the tool library and the list of timezones. Any other API route without the
  organisation stops the app starting. `tests/test_organisation_prefix.py` is generated from
  the router: every prefixed route answers a non-member 404, and only those listed stay
  unscoped. The backend tests' client sends an organisation's routes under the prefix, in the
  signed-in user's organisation (`OrganisationClient` in `tests/conftest.py`).
- **Front end.** Every signed-in page is under `/o/<slug>/`, the router's basename for as long as
  the page is open (`frontend/src/lib/organisation-path.ts`), so the app's own links stay in it.
  A path without one (an old bookmark, the root) opens the same page in the organisation used
  most recently, remembered in the browser's storage (a short list, most recent first, every
  access guarded), else in the one joined most recently; a person in none goes to the welcome
  page. A link to an organisation the user isn't in shows "Organisation not found", with links
  to theirs and to create one. The
  page sends the API its organisation's id (`organisationApi` in `frontend/src/lib/api.ts`, set
  once the slug is matched to a membership), so two tabs on two organisations work
  independently. The browser tests' fake API (`frontend/e2e/fake-api.ts`) answers only under
  the prefix too.

`GET /api/me` lists the user's organisations (`organisations`: id, name, slug and whether
they're its creator, the one joined most recently first; empty for a person in none). The shell
shows the name of the organisation the page is open in as the **switcher**: a menu of every
organisation the person is in, where another opens in its own link (a new page load), the
current one opens its **Organisation** page, and **Create an organisation** goes to the welcome
page. The Organisation page
(`GET /api/o/{org}/organisation`): the name and the creator above three tabs, as on the agent page,
each a route: **Members**, with when each joined and the count (`/organisation`);
**Invitations**, the links, for the creator only (`/organisation/invitations`); and
**Settings**, the approval rules, leaving and deleting it (`/organisation/settings`). Only the creator
renames it (`PUT /api/o/{org}/organisation`; a 403 for another member, a 409 for a name another
organisation has): a stopgap role until roles exist. The **Settings** page is the user's own
and links to it.

### What the organisation owns

**Agents, skills, workflows and schedules** belong to the organisation: every member lists,
reads, edits, versions, restores, runs and archives every one, whoever made it, and to a user in
another organisation each is a 404. Agents, skills and workflows are in the organisation's store,
whose ids are its own (another organisation's store may use the same ones), and their endpoints
read only that store (`CurrentStore`; see [Stores](stores.md#agents-skills-and-workflows)); a
schedule's `get_org_trigger` checks its `organisation_id` against `CurrentOrganisation`, and
the lists filter by it. Names are unique within the
organisation (a 409 names the clash), so two organisations can each have a "Reviewer". Each
says which member made it and which last changed it (`created_by`, `updated_by`, reported as
the person, shown on lists and pages: `aios.api.provenance`, resolved through the store's people
mirror, see [Stores](stores.md#the-people-mirror); someone who has left shows as a "former
member"), each version which member saved it,
and a deleted one records who deleted it (`deleted_by`). A workflow or schedule can only run
the organisation's agents and workflows, and deleting an agent finds what depends on it across
the organisation. Nothing answers to whoever made it. The **store** is the organisation's (see
[Stores](stores.md)), and so is its search index, which every member searches.

### Runs belong to the organisation

**Runs** execute for the organisation, however they start: by hand, by a schedule, by a chat
mention, or as a workflow's agent node. A run's tools use the organisation's store, its trace
carries the organisation and who started it (see [Tracing](tracing.md)), and nothing about it
depends on whoever made the agent or workflow, so a schedule or a mention goes on working after
they've left. Each agent run and workflow run records the member who started it (`created_by`:
who started it by hand or mentioned it; a workflow's agent nodes' runs take the workflow run's),
null for a schedule's run, which records its schedule (`trigger_id`) instead; run lists and run
pages show the member, or "Scheduled". Every member reads, continues and deletes every run in
the organisation, the ones chats and schedules started included, and to anyone else each is a
404.

## Invitation links

The creator brings people in with **invitation links**, made on the Organisation page's
Invitations tab (`/organisation/invitations`; `POST /api/o/{org}/organisation/invites`,
`expires_in_days` 1 to 30, a week by default): the response carries the link's random token
once, and only its hash is stored, so the page shows the link to copy and never again. `GET`
lists the open links (who made each, when it expires) and `DELETE .../invites/{id}` revokes
one; all three are the creator's (a 403 for another member). The link opens the **invite
page** (`/invite/<token>`; `GET /api/invites/{token}`, no sign-in needed), which names the
organisation; an expired or revoked link is a 404 and says so. Signed out, it offers to
register (`/register?invite=<token>`, which sends the token with the registration: the new user
joins that organisation and only that one, the membership recording the link, shown beside the
member; a link that no longer opens joins them to nothing) or to sign in (`/login`); either
comes back to the invite page.

Signed in, Accept (`POST /api/invites/{token}/accept`) adds a membership of the link's
organisation, whatever else the person is in, and the page opens it. Accepting a link into an
organisation they're in already answers the same (200, the organisation) and adds nothing, so a
second click, or two at once, is harmless; the invite page says they're a member, with a link to
it.

## Leaving

A member **leaves** from the Organisation page's Settings tab (`/organisation/settings`;
`POST /api/o/{org}/organisation/leave`): they drop out of the organisation's chats (an add, a join or a
new chat of theirs at the same moment is a 404, not a way back into one), their unread
notifications about its schema change requests go, and nothing else: no organisation is made
for them. The page lands them in the organisation they used most recently of those they're still
in, or on the welcome page if none remain. What they made stays, and a chat they owned goes to its
chat member added first (with nobody left in it, a public one goes to whoever joins it next,
and a private one, which nobody could join, is deleted, as its owner deleting it would be).
The last member can't leave, and is told to delete the organisation instead; nor can the creator
while others remain, since nobody else could invite, and there's no transfer until roles exist
(each a 409, explained on the page).

## Deleting an organisation

Only the creator deletes an organisation (admins will, once phase 1 adds roles), from the
Organisation page's Settings tab, by typing its name (`POST /api/o/{org}/organisation/deletion`,
`{"name": ...}`: a 422 for another name, a 409 if it's scheduled already, a 403 for another
member). It isn't deleted then: it's deleted after a **grace period**,
`ORGANISATION_DELETION_GRACE_DAYS` (14 by default). Every member is notified
("organisation deletion scheduled"), and a banner on every page shows the date (`deletes_at`
on each organisation `/api/me` lists; the Organisation page's `deletion` says who scheduled it
and when).

During the grace period the organisation works as before, but its invitation links don't open
(the invite page says the link has expired or been revoked; one made then doesn't open either).
The creator can **cancel** (`DELETE .../organisation/deletion`, a 409 if none is scheduled):
nothing is touched, the links open again until they expire, and every member is notified again.

The provisioning worker's beat looks for organisations whose grace period is over every
`ORGANISATION_DELETION_CHECK_SECONDS` (10 minutes by default; `aios.delete_due_organisations`,
`aios.organisations.delete_due`). For each, in one transaction, it records the deletion in
`organisation_deletions` (the name, slug, who scheduled it, when, when it went, and its store),
detaches the store, which goes `deleting` for the existing teardown to drop with its files and
roles (`aios.datastores.provision.delete_deleting`, queued at once and swept on its own
schedule too), and deletes the organisation, cascading to everything in it. Members with no
organisation left land on the welcome page; a link to the deleted one says it isn't found to
anyone still in another.
