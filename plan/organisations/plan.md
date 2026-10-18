# Organisations

Status: **plan** (2026-10-18). Draft for discussion: the open questions at the end decide
several of the steps, and the plan says which assumption each step is written under.

## Goal

Users belong to organisations, and everything a user makes today belongs to an organisation
instead: agents, workflows, skills, schedules, chats, runs and the data store. Only an
organisation's members can see or use any of it. Users receive and accept invitations to
organisations, and can leave one. Roles and permissions within an organisation are out of
scope: every member can do everything.

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

## Design

### The model

```
organisations
  id              serial
  name            text, unique among live organisations
  created_by      -> users (SET NULL)
  created_at, updated_at

organisation_members
  organisation_id -> organisations (CASCADE)
  user_id         -> users (CASCADE)
  joined_at
  primary key (organisation_id, user_id)

organisation_invites
  id              uuid
  organisation_id -> organisations (CASCADE)
  invitee_login   text        -- the GitHub login invited (see Q3 for the alternatives)
  invited_by      -> users (SET NULL)
  created_at, expires_at
  accepted_at, accepted_by -> users (SET NULL)
  declined_at, revoked_at
  unique (organisation_id, invitee_login) where open
```

Membership is a table, so a user can be in several organisations (Q1 is whether the UI lets
them switch yet; the model doesn't change either way). There is no role column: an
`organisation_member` row is the whole permission.

Everything that has `owner_id` today gets `organisation_id` instead, plus `created_by` (nullable,
`SET NULL`) for provenance:

| Table | Today | After |
| --- | --- | --- |
| `skills`, `agents`, `workflows`, `triggers`, `chats` | `owner_id -> users` | `organisation_id -> organisations (CASCADE)`, `created_by -> users (SET NULL)` |
| `data_stores` | `owner_user_id -> users`, unique | `owner_organisation_id -> organisations (CASCADE)`, unique |
| `agent_runs`, `workflow_runs` | owned through the agent/workflow | unchanged, plus `created_by -> users (SET NULL)` so members can tell their runs apart (null for a schedule's) |
| `users` | `export_run_history`, `embed_run_history` | move to `data_stores` (they're settings of the store, decided by its owner, which is now the organisation) |
| `notifications` | per user | unchanged: still per user, fanned out to every member who may act |

The unique-name indexes (`uq_agents_owner_id_name_live`, skills, workflows) become per
organisation, as does `uq_agents_owner_id_builtin_live`: one live documents assistant per
organisation, over the organisation's documents. `chats.name` stays unique per organisation
rather than across the deployment.

Cascades change meaning. Today deleting a user deletes their agents, workflows, runs and store;
after, deleting a user leaves all of that (it's the organisation's) and only `SET NULL`s
`created_by`. Deleting an organisation is what cascades. The deferred foreign keys on
`workflow_nodes` were reasoned about in terms of user deletion ("only deleting a user removes
them"); the same reasoning holds for organisation deletion, and the comments move with it.

### Request context

`CurrentUser` stays what it is. A new dependency, `CurrentOrganisation`, resolves the
organisation the request is about and checks the user is a member (404 otherwise, as a private
chat is for a non-member today). Under Q1's recommendation, the SPA names the organisation on
each request and the API never guesses:

- the frontend keeps the organisation in the URL (`/o/:orgId/agents/...`), so two tabs can be
  in two organisations and a link says which one it's about;
- the API client sends it as a header (`X-Organisation`) on every call under `/api`, so no
  existing API path changes; `CurrentOrganisation` reads the header.

`CurrentDataStore` becomes the organisation's store (`data_store_for(org_id)`). The
`get_owned_*` helpers become `get_org_*`: the check is `row.organisation_id == org.id` instead
of `row.owner_id == user.id`, and list queries filter by organisation. Every endpoint that
takes `CurrentUser` only to scope a query takes `CurrentOrganisation` instead; `CurrentUser`
remains for `created_by`, notifications, chat membership and `/api/me`.

Endpoints that aren't about one organisation stay unscoped: `/api/me` (now with the user's
organisations and open invitations), `/api/organisations` (create, list mine),
`/api/invites` (mine), `/api/notifications` (every organisation's, each carrying its
`organisation_id` in `ref` so the SPA can link into the right one).

### Execution identity

A run "executes as its agent's owner" today: `ChatDeps.user_id` is the owner, the store is
the owner's, traces carry the owner's id, and the scheduler, workflow worker, handoff and chat
invocations all pass `agent.owner_id` or `workflow.owner_id` around. After:

- a run executes on behalf of the organisation: its store is the organisation's
  (`data_store_for(agent.organisation_id)`), and the organisation's id goes in the trace
  metadata;
- `ChatDeps.user_id` becomes the user who started the run (`created_by`), or null for a
  schedule's run. Nothing in the tools reads it (checked: they read `data_store_id` and
  `run_id`), so this is a rename of intent, and the places that build a `Turn`
  (`scheduler._start_agent_run`, `chats.TurnFiring`), a `RunState` (`workflows.claim`) or the
  handoff's trace metadata pass the organisation and the starter instead of the owner;
- `load_chat_context(db, user_id, ...)` takes the organisation, and `get_owned_run` becomes
  `get_org_run`;
- the chat run-sharing rule (`invocations.shares_agent_run`: a chat member may read a run a
  mention started) becomes redundant, since chats are inside the organisation and every member
  may read every run. It can go, with `is_owner` on runs; the chat's own `owner` role stays
  (it's a chat permission, not an ownership of data).

### The data store

- `data_store_for(org_id)` replaces `data_store_for(user_id)`; the function body is the same
  with the column renamed, and still the only place an organisation becomes a store. A store is
  created when the organisation is (as it is on sign-in today), and provisioned by the same
  worker.
- The run history export matches runs to stores through `Agent.owner_id -> User ->
  DataStore.owner_user_id` in five queries in `datastores.history`; each becomes
  `Agent.organisation_id -> DataStore.owner_organisation_id`, one join fewer. Its on/off
  settings move from `users` to `data_stores`, and the Settings page's export section moves to
  an organisation page.
- `approvers(store)` returns the organisation's members. Schema change requests, dropped-object
  restores and their notifications follow from that with no other change.
- Quotas, provisioning, migrations, credentials and the sandbox are untouched: all per store.
- The documents assistant's description ("Answers from your documents") and the tools'
  docstrings that say "the user's documents" say the organisation's.

### Invitations and membership

- `POST /api/organisations/{id}/invites` `{login}`: any member may invite (no roles). Refused
  if the login is already a member or already has an open invitation.
- `GET /api/organisations/{id}/invites`: open ones; `DELETE .../invites/{id}` revokes.
- `GET /api/invites`: the signed-in user's open invitations, matched on their GitHub login. On
  sign-in, `auth_callback` already refreshes the login, so an invitation made before the user
  first signed in is waiting for them. The SPA shows them on the Organisations page and as a
  notification (`kind = "organisation_invite"`).
- `POST /api/invites/{id}/accept` adds the membership; `POST .../decline` closes it. Both only
  for the invitee.
- `POST /api/organisations/{id}/leave` removes the member, their memberships of the
  organisation's chats, and their unread notifications about it. The last member can't leave
  (409): an organisation with no members would still run its schedules against a store nobody
  can see (Q5).
- `POST /api/organisations` creates one, with the creator as its first member and a new
  store. `GET /api/organisations/{id}/members` lists members.
- Nothing is sent off-app, as for chats. Email can come later; the invitation row doesn't care
  how the invitee heard.

### Frontend

- `api.me()` returns the user with their organisations; an `OrganisationProvider` under the
  router reads `:orgId` from the URL, checks it's one of theirs (else redirects to the first,
  or to the Organisations page when they have none) and gives the API client the header.
- Routes move under `/o/:orgId/` (agents, workflows, skills, scheduler, chats, data, search).
  `/` redirects to the last-used organisation's chats (remembered in `localStorage`), or to
  `/organisations`.
- An organisation switcher in the shell's header, and an **Organisations** page: the user's
  organisations, create one, open invitations with accept/decline, and per organisation its
  members, invite form, open invitations and Leave.
- Settings splits: the run history export becomes the organisation's (under `/o/:orgId/settings`
  or the Data page's Overview); the user's Settings page keeps anything per user (nothing yet).
- Lists and detail pages are unchanged otherwise, since the API paths don't change. Run lists
  show who started each run.
- `frontend/e2e/fake-api.ts` grows organisations, memberships and invitations, and every spec
  signs in with one organisation.

### Migration

Three Alembic revisions, in one release, with a data step between schema steps:

1. **0029, expand.** Create `organisations`, `organisation_members`, `organisation_invites`.
   Add nullable `organisation_id` and `created_by` to `skills`, `agents`, `workflows`,
   `triggers`, `chats`; `created_by` to `agent_runs`, `workflow_runs`; `owner_organisation_id`
   to `data_stores`; the export flags to `data_stores`.
2. **0030, data.** For each user, a personal organisation named after their login (deduplicated
   with a suffix on a clash), with them as its one member; set every owned row's
   `organisation_id` from its `owner_id`, `created_by = owner_id`; re-point each store at the
   owner's organisation and copy the two export flags. Users with no store yet (none in
   practice: sign-in makes one) get one made for their organisation.
3. **0031, contract.** `NOT NULL` on the new columns; drop `owner_id`, `owner_user_id` and the
   users' export flags; replace the four unique-name indexes with per-organisation ones.

Contracting in the same release goes against expand-then-contract, as revision 0015 did and
said why: between the migration and the end of the rollout, the previous release's requests
fail on the missing columns. The alternative is to keep `owner_id` one release longer, written
by both releases, and drop it in the next. Q7 asks which.

The store tree is untouched: nothing in a store's database names its owner. The LangGraph
checkpoint threads are named after run ids, so they're untouched too.

### Tests

- `conftest`'s `user`, `provisioned_user` and `new_user` fixtures make the user's organisation
  and provision *its* store; `delete_user` deletes the organisation (which cascades to the
  store, agents and the rest) and then the user. The `api` fixture also overrides
  `current_organisation`.
- Every test that creates an `Agent(owner_id=...)` and the like sets `organisation_id`
  (sixteen test modules reference `owner_id` or the fixtures today).
- New tests: membership checks (a member of another organisation gets 404 on each resource
  kind, and on runs, chats and documents), invitations (invite, accept, decline, revoke,
  duplicate, non-invitee), leaving (last member refused; chat memberships and notifications
  cleared), the data store resolving to the organisation's in a run started by a member, by a
  schedule and by a chat mention, approvals and notifications fanning out to every member, the
  export settings living on the store, and the migration's data step on a fixture of two users
  with shared names.
- Browser tests: organisation switching, the Organisations page, an invitation accepted from
  the notifications menu.

## Steps

Each is a PR; the order keeps the app working after each one.

1. **Organisations, memberships and invitations, alongside what exists.** Models, 0029, the
   organisation and invitation endpoints, the Organisations page. Every existing user gets a
   personal organisation on sign-in (and 0030's data step for existing ones). Nothing is
   scoped yet; the feature is inert.
2. **Request context.** `CurrentOrganisation`, the header, the URL scheme, the switcher.
   Each API module moves from `owner_id` to `organisation_id` behind it, reading
   `organisation_id` and still writing both columns.
3. **The data store.** `data_store_for(org_id)`, `approvers`, the history export's joins and
   settings, the Settings page split.
4. **Execution.** Scheduler, workflow worker, handoff, invocations and tracing pass the
   organisation and the starter; `created_by` on runs; the run-sharing rule removed.
5. **Chats.** Scoped and named per organisation; members must be organisation members;
   `leave` clears chat memberships.
6. **Contract.** 0031, the `get_owned_*` names gone, README and docstrings updated.

## Open architectural questions

1. **One organisation per user at a time, or several?** The membership table allows several;
   the question is the request context. *Recommendation:* several, with the organisation named
   in the URL and sent as a header, as above. The alternatives: an organisation chosen per
   session (`POST /api/me/organisation`, kept in the cookie), which keeps the SPA's URLs as they
   are but makes links ambiguous and ties every tab to one organisation; or organisation ids in
   the API paths (`/api/organisations/{id}/agents`), which is the most explicit but changes
   every path and every client call.

2. **Does every user get a personal organisation?** *Recommendation:* yes, made on first
   sign-in and named after their login, so the app works before anyone invites them and so the
   migration of existing users is the same code path. The alternative is invitation-only: a
   new user lands on an empty Organisations page until invited, and someone has to bootstrap
   the first organisation (a `python -m aios.organisations create` command, or the first user
   to sign in). Personal organisations also raise whether a user may delete or rename theirs;
   this plan leaves organisation deletion out.

3. **How is an invitee identified?** Auth is GitHub OAuth, and `users.email` is null for
   accounts with a private email, so email is an unreliable key, and the app sends no email
   anyway. *Recommendation:* by GitHub login, matched when the invitee signs in (the login is
   refreshed on every sign-in). Alternatives: by email as well, matched against the verified
   primary email; or a shareable link with a token, accepted by whoever is signed in when they
   open it, which needs no identity but is a bearer secret. Logins can be renamed on GitHub;
   an invitation to a renamed login simply never matches and expires.

4. **Who may invite, revoke and remove?** With no roles, *recommendation:* any member may
   invite and revoke invitations, and nobody may remove another member (only leave). The
   creator has no special standing; whoever adds roles later decides otherwise. Is that
   acceptable until roles exist, or should the creator alone invite as a stopgap? (That is a
   role in all but name.)

5. **What happens to the last member's organisation?** *Recommendation:* the last member
   can't leave (409), and organisation deletion is out of scope. Alternatives: leaving as the
   last member deletes the organisation and its store (the store's `deleting` status exists
   for this and is "not yet used"), or the organisation is left empty with its schedules
   disabled. Deletion needs the same dependents reasoning as deleting an agent
   (`aios.dependents`), at the whole-organisation scale, and the store's database dropped.

6. **Who does a run execute as?** Today the agent's owner, and traces say so. The plan makes
   runs the organisation's, started by a member or a schedule, and drops the per-user reading
   rule. Is any per-user visibility wanted inside an organisation (a member's private runs or
   chats), or is everything in an organisation visible to every member until roles arrive?
   The plan assumes the latter, which is also what "don't worry about roles" implies.

7. **Contract in the same release, or the next?** Dropping `owner_id` while the previous
   release is still serving breaks its requests for the minute or two of the rollout, as
   revision 0015 did knowingly. Keeping `owner_id` a release longer means writing both columns
   in this release and a follow-up PR to drop them. *Recommendation:* same release, as for
   0015, unless the deployment is now serving people who would notice.

8. **Where do the user's remaining settings live?** With the export flags moved to the store,
   the user has no settings. Keep the Settings page (empty, for later per-user settings), or
   replace it with the Organisations page and put the export on the organisation's Data page?

9. **Naming and identity in URLs.** Organisation ids in URLs (`/o/12/agents`) or slugs
   (`/o/acme/agents`)? Slugs read better and survive in links, but need a unique, renamable
   slug column and a redirect on rename. *Recommendation:* ids now; a slug can be added later
   without changing the API.

10. **Chats and organisations.** A chat's name is unique across the deployment today. Per
    organisation after, which the migration keeps unique trivially (each chat moves to one
    organisation). Should an organisation's public chats be joinable by every member (yes,
    under this plan), and is cross-organisation chat ever wanted? The plan assumes not.
