# Organisations

Status: **decided** (2026-10-18). The open questions of the first draft were answered; the
answers are under [Decisions](#decisions), and the design below is written to them. The spec and
tickets are on GitHub (the spec issue links back here).

## Goal

Users belong to organisations, and everything a user makes today belongs to an organisation
instead: agents, workflows, skills, schedules, chats, runs and the data store. Only an
organisation's members can see or use any of it. Users join an organisation through a
shareable invitation link, and can leave one. Roles and permissions within an organisation are
out of scope: every member can do everything, except that only the organisation's creator can
make invitation links (a stopgap until roles exist).

## What the codebase already gives us

The design has been expecting this. Each place names the hook it left:

- **Data stores already have an id of their own.** `DataStore` says a store "can later change
  hands (to a team) without renaming anything named after it": the database and roles are
  `ds_<store id>`, never named after a user. Moving a store to an organisation is changing one
  column. `data_store_for(user_id)` is "the one place a user becomes a data store", and
  "everything below this takes a data store id, not a user: when teams own stores, only how a
  caller picks its store changes".
- **Tools never see the user.** `ChatDeps.data_store_id` is resolved when a turn starts, "so
  they needn't change when a run's store isn't its user's (teams)". The data, search, history
  and schema change tools, and the sandbox, take the store id.
- **Approvals are a permission, not an identity.** `resolve.approvers(store)` returns the users
  who may decide a store's schema change requests (and restore dropped tables), "so teams can
  give it to more than the owner". Notifications go to that list.
- **Chats anticipate narrowing.** "Public" means every signed-in user of this deployment;
  `aios.api.chats` says "organisations will narrow it".
- **Ownership is one column per table**, `owner_id` on `skills`, `agents`, `workflows`,
  `triggers` and `chats`, with `get_owned_*` helpers in each API module as the only checks.
- **Store deletion has a status waiting for it.** `DataStoreStatus.deleting` and `deleted` are
  "for deleting users' stores (not yet used)".

## Decisions

The first draft's ten questions, as answered:

1. **One organisation per user at a time.** Membership is a table all the same, so several per
   user can come later without a schema change; a unique constraint on the user holds it to
   one for now, and the request context is resolved in one place (`CurrentOrganisation`), the
   one function to change when a request has to name its organisation.
2. **A personal organisation on sign-up without an invitation**, named after the user's login.
   Signing up through an invitation link joins that organisation instead.
3. **Invitations are shareable links**, carrying a random token. Anyone who opens one and signs
   in joins. No email is sent.
4. **Only the creator** makes and revokes invitation links, and renames the organisation. A
   role in all but name; roles will replace it.
5. **The last member can't leave.** Nor can the creator while others remain (nobody else could
   invite). Organisation deletion is out of scope, with one exception below.
6. **Everything in an organisation is visible to every member.** The per-user run-sharing rule
   for chats goes.
7. **`owner_id` is dropped in the same release**, as revision 0015 did, on the condition that
   every create, update and delete of a record can be traced to a user in the organisation:
   `created_by` and `updated_by` on every identity row, `deleted_by` on the soft-deleted ones,
   `created_by` on runs (null for a schedule's), `uploaded_by` on documents.
8. **A user settings page and an organisation settings page.** The run history export moves to
   the organisation's.
9. **Ids**, not slugs, wherever an organisation is named in a URL.
10. **No cross-organisation chats.**

One consequence had to be settled to make 1, 2 and 5 hold together. A user who signed up
without an invitation is the only member of a personal organisation, and must leave it to
accept an invitation, which 5 refuses. So: **accepting an invitation while a member of another
organisation is refused (409) unless that organisation is empty**, holding nothing but its
member and the defaults (no agents other than built-in ones, no workflows, skills, chats,
schedules, documents or data tables); an empty one is deleted on accepting, with its store. A
user with things in their personal organisation deletes them first (or keeps the organisation
and declines). Leaving a non-personal organisation as an ordinary member puts the user in a new
personal organisation, so every user is always in exactly one.

## Design

### The model

```
organisations
  id              serial
  name            text, unique
  personal        bool        -- made on sign-up for one user; eligible for deletion when empty
  created_by      -> users (SET NULL)   -- the creator: may invite, revoke and rename
  created_at, updated_at

organisation_members
  organisation_id -> organisations (CASCADE)
  user_id         -> users (CASCADE), unique for now (decision 1)
  invite_id       -> organisation_invites (SET NULL)   -- the link they came through, if any
  joined_at
  primary key (organisation_id, user_id)

organisation_invites
  id              uuid
  organisation_id -> organisations (CASCADE)
  token_hash      text, unique      -- the link carries the token; only its hash is stored
  created_by      -> users (SET NULL)
  created_at, expires_at (default 7 days, at most 30), revoked_at
```

Everything that has `owner_id` today gets `organisation_id` instead, with the provenance
columns decision 7 asks for:

| Table | Today | After |
| --- | --- | --- |
| `skills`, `agents`, `workflows`, `triggers`, `chats` | `owner_id -> users` | `organisation_id -> organisations (CASCADE)`; `created_by`, `updated_by -> users (SET NULL)`; `deleted_by` on the three with `deleted_at` |
| `data_stores` | `owner_user_id -> users`, unique | `owner_organisation_id -> organisations`, unique, nullable only while `deleting` |
| `agent_runs`, `workflow_runs` | owned through the agent/workflow | plus `created_by -> users (SET NULL)`, null for a schedule's run |
| `users` | `export_run_history`, `embed_run_history` | moved to `data_stores` |
| store tree: `documents` | | `uploaded_by` (a user id, no foreign key: stores have no users table) |
| `notifications` | per user | unchanged, fanned out to every member who may act |

The unique-name indexes (`uq_agents_owner_id_name_live`, skills, workflows) become per
organisation, as does `uq_agents_owner_id_builtin_live`: one live documents assistant per
organisation, over the organisation's documents. `chats.name` becomes unique per organisation.

Cascades change meaning. Deleting a user no longer deletes agents, workflows, runs or a store
(they're the organisation's); it `SET NULL`s the provenance columns. Deleting an organisation
cascades to all of it. The deferred foreign keys on `workflow_nodes` were reasoned about in
terms of user deletion; the same reasoning holds for organisation deletion.

### Request context

`CurrentUser` stays. `CurrentOrganisation` is the signed-in user's one membership, resolved by
one dependency; a user always has one (sign-up and leaving both guarantee it). No header, no
organisation in the URL: when several memberships are allowed, this dependency is where the
request's organisation will be read from, and URLs will carry its id (decision 9).

`CurrentDataStore` becomes the organisation's store. The `get_owned_*` helpers become
`get_org_*`: `row.organisation_id == org.id` instead of `row.owner_id == user.id`, and list
queries filter by organisation. Endpoints take `CurrentOrganisation` to scope and `CurrentUser`
for provenance, notifications, chat membership and `/api/me`, which now also returns the
user's organisation.

### Execution identity

A run "executes as its agent's owner" today. After, it executes on behalf of the organisation:
its store is the organisation's, its trace carries the organisation's id and `created_by`, and
`ChatDeps.user_id` becomes the user who started the run, or null for a schedule's (nothing in
the tools reads it: they read `data_store_id` and `run_id`). The scheduler, the workflow worker,
the handoff and chat invocations pass the organisation and the starter instead of the owner.
`invocations.shares_agent_run` and `is_owner` on runs go: every member reads every run.

### The data store

- `data_store_for(org_id)` replaces `data_store_for(user_id)`, still the only place an
  organisation becomes a store. A store is created with the organisation.
- The run history export matches runs to stores by `organisation_id` instead of through
  `users`; its two flags move to `data_stores`; the organisation settings page gets the export
  section the user settings page has today.
- `approvers(store)` returns the organisation's members. Requests, dropped-object restores and
  their notifications follow.
- **Deleting a store**, for the empty-organisation case: the store goes `deleting` and loses its
  organisation; the provisioner's sweep drops its database and roles, deletes its files under
  its prefix, and marks it `deleted` (or removes the row). Engines on it are disposed first.

### Invitation links and membership

- `POST /api/organisation/invites` (creator only) makes a link: the response carries the token
  once (`PUBLIC_URL/invite/<token>`); `GET` lists open links by id, creator and expiry;
  `DELETE .../invites/{id}` revokes.
- `GET /api/invites/{token}` (no sign-in needed) says which organisation the link is for, and
  whether it's still open, so the invite page can show it.
- Opening `/invite/<token>` signed out keeps the token in the cookie session through the
  GitHub round trip; the callback finds it, joins the organisation (a new user gets no
  personal organisation) and lands in it. Signed in, the page shows the organisation and
  Accept: `POST /api/invites/{token}/accept`, which applies the rule in Decisions (409 unless
  the current organisation is empty, which is then deleted).
- `POST /api/organisation/leave`: refused for the last member and for the creator while others
  remain; otherwise removes the member, their memberships of the organisation's chats and
  their unread notifications about it, and puts them in a new personal organisation.
- `GET /api/organisation` (name, creator, members, whether the caller is the creator);
  `PUT /api/organisation` renames (creator only). Creating a second organisation by hand is
  out of scope: an organisation is made on sign-up, by leaving, or by the migration.

### Frontend

- `api.me()` returns the user and their organisation; the shell shows the organisation's name.
  No route changes for the scoped pages.
- **Organisation** page (`/organisation`): name (editable by the creator), members with when
  they joined, invitation links (make, copy, revoke; creator only), the run history export
  section, and Leave with the rules explained.
- **Settings** (`/settings`) keeps the user's own settings; nothing per user remains after the
  export moves, so it shows the account and links to the organisation page.
- **Invite** page (`/invite/:token`): the organisation's name and Accept (or sign in first);
  the 409 explains what to clear out, linking to the pages.
- Lists and detail pages show who created, last changed or started each thing.
- `frontend/e2e/fake-api.ts` grows the organisation, members and invitation links.

### Migration

Three Alembic revisions in one release (expand, data, contract), as revision 0015 contracted in
the same release and said why:

1. **Expand.** The three new tables; nullable `organisation_id`, `created_by`, `updated_by`
   (and `deleted_by` where there's `deleted_at`) on the five owned tables; `created_by` on
   runs; `owner_organisation_id` and the export flags on `data_stores`.
2. **Data.** A personal organisation per user, named after their login (suffixed on a clash),
   `created_by` them, with them as its member; every owned row's `organisation_id` and
   `created_by` from its `owner_id`; each store re-pointed at its owner's organisation with
   the flags copied.
3. **Contract.** `NOT NULL` on `organisation_id`; drop `owner_id`, `owner_user_id` and the
   users' flags; per-organisation unique indexes.

Store tree: one revision adding `documents.uploaded_by` (nullable; existing rows stay null).

### Tests

- `conftest`'s `user`, `provisioned_user` and `new_user` fixtures make the user's organisation
  and provision its store; `delete_user` deletes the organisation (cascading) and the user.
  The `api` fixture also overrides `current_organisation`.
- Every test that writes `owner_id` sets `organisation_id` and `created_by`.
- New tests: membership checks (another organisation's member gets 404 on each kind, on runs,
  chats and documents); invitation links (make, open, accept, sign up through one, revoke,
  expiry, creator-only, the 409 and the empty-organisation deletion); leaving (last member and
  creator refused; a new personal organisation; chat memberships and notifications cleared);
  the store resolving to the organisation's in runs started by hand, by schedule and by
  mention; approvals and notifications to every member; the export flags on the store; the
  migration's data step on two users with clashing names; store deletion.
- Browser tests: the Organisation page, an invitation link accepted, the invite page signed out.
