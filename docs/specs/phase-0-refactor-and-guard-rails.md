# Phase 0: Refactor and guard rails

This spec covers plan steps 0.1 to 0.6: docs by domain, modules by domain, the full-stack test,
sharded test runs, archive instead of delete, and many organisations per person. It follows
[decision record 0001](../decisions/0001-organisation-database-as-system-of-record.md) (R2, R3,
D11), [the plan](../../plan/platform-cohesion/plan.md) and [the glossary](../../CONTEXT.md).

## Problem Statement

**Members, admins and editors**
- A person can be in only one organisation. Signing up quietly makes a "personal" organisation
  with its own store.
  - Accepting an invitation is refused unless that personal organisation is still empty. The
    person is then told to delete their agents, chats and documents first.
  - Leaving an organisation drops them into a new personal one they never asked for.
  - Someone who works with two companies, or two departments kept apart, can't be in both.
- Nothing in a link says which organisation it's about. Two browser tabs can't hold two
  organisations.
- Deleting an agent is an ordeal. A dialog lists every chat run, workflow run, workflow and
  schedule that depends on it. Each must be cancelled, deactivated or edited away before Delete
  enables, and the list is re-checked when the window regains focus.
  - Deleting also throws away the agent's conversations, threads and sandboxes, and stops turns
    mid-flight.
  - Skills and workflows can't be deleted at all while something uses them.
  - People who only want an agent to stop working have no gentle way to say so.
- An organisation can't be deleted on purpose. The only deletion path is the side effect of
  accepting an invitation.

**Developers maintaining aios**
- The README is over a thousand lines of dense prose and serves as the only spec. Some lines no
  longer match the code.
- Five backend modules are each over a thousand lines. Every change in phases 1 to 4 touches
  them.
- The core loop has no test exercising the real API, workers, database and browser together:
  mention, run, schema change, approval, resume, reply. Its parts are tested separately, against
  a fake API in the browser.
- CI runs every backend test in one job and every browser test on one machine. CI time will grow
  with every step that follows.

## Solution

**For people using aios**
- People join organisations rather than being placed in one.
  - Signing up makes nothing. A new person sees a welcome page that creates an organisation or
    explains invitation links.
  - Accepting an invitation simply adds a membership.
  - Leaving simply removes one.
  - A person can be in any number of organisations and switch between them from the shell.
- The organisation is part of every link, so a shared link opens the right one and two tabs can
  work in two organisations.
- Agents, skills and workflows are **archived** instead of deleted.
  - Archiving is one confirmation, never blocked. It shows a read-only list of what uses the
    item.
  - An archived item starts nothing new. Anything already going finishes, and nothing is thrown
    away.
  - Unarchiving brings it back.
- An organisation's creator can **delete the organisation** deliberately. Members are told, and
  there's a grace period in which it can be cancelled.

**For developers**
- The README becomes an overview and quick start, and the rest moves to one page per domain.
- The large modules are split by domain, with models grouped along the boundary phase 1 moves
  across.
- A full-stack browser test runs the core loop against the real stack, with a scripted model.
- The backend, browser and full-stack suites are sharded across CI machines, balanced on recorded
  durations, and run in random order to keep tests independent.

## User Stories

### Joining, creating and switching organisations

1. As a person signing up without an invitation, I want to land on a welcome page rather than in
   an organisation made for me, so that nothing exists in my name that I didn't ask for.
2. As a person on the welcome page, I want to create an organisation by giving it a name, so that
   I can start working straight away.
3. As a person on the welcome page, I want to be told that an invitation link adds me to an
   organisation, so that I know how to join my colleagues.
4. As a person creating an organisation, I want its store provisioned at once, so that I can
   upload documents as soon as it's ready.
5. As a person creating an organisation with the same name as another company's, I want that to
   be allowed, so that common names aren't taken by strangers.
6. As a person signing up through an invitation link, I want to join that organisation and only
   that one, so that I start where I was invited.
7. As a member of one organisation, I want to accept an invitation to another and keep my first
   membership, so that I can work with both.
8. As a member with work in my current organisation, I want accepting an invitation never to be
   refused because of it, so that I don't have to delete anything to join.
9. As a member who clicks an invitation link twice, I want the second accept to succeed quietly,
   so that I'm not shown an error for something that worked.
10. As a member of several organisations, I want a switcher in the shell listing them all, so
    that I can move between them in one click.
11. As a member, I want **Create an organisation** in the switcher, so that I can start another
    without leaving my work.
12. As a member, I want every page's link to name its organisation, so that a link I share opens
    the same organisation for my colleague.
13. As a member, I want two tabs open on two organisations to work independently, so that posting
    in one never lands in the other.
14. As a member opening an old bookmark without an organisation in it, I want to be taken to the
    same page in the organisation I used last, so that old links keep working.
15. As a member opening a link to an organisation I'm not in, I want a "not found" page with my
    switcher, so that I can't learn whether it exists, and can get back to my own.
16. As a member, I want the screens to say "Organisation", so that the word matches what it is.
17. As an organisation's creator, I want its link to stay the same when I rename it, so that
    shared links don't break.
18. As a member, I want notifications from all my organisations in one bell, each saying which
    organisation it's from, so that I don't miss anything while switched elsewhere.

### Leaving

19. As a member, I want leaving to remove only my membership, so that I don't end up in a new
    organisation I didn't ask for.
20. As a member who leaves, I want to land in the organisation I used most recently, or on the
    welcome page if I have none left, so that I always have somewhere to go.
21. As a member who leaves, I want my chats in that organisation to be handed over as they are
    today, so that the people still there can carry on.
22. As an organisation's last member, I want to be told to delete it rather than leave it, so that
    it isn't left with nobody able to see it.
23. As an organisation's creator with others still in it, I want to be told I can't leave yet, so
    that it isn't left without anyone able to manage it before roles exist.

### Deleting an organisation

24. As an organisation's creator, I want to schedule its deletion by typing its name, so that I
    can't do it by accident.
25. As an organisation's creator, I want a grace period before anything is destroyed, so that a
    mistake can be undone.
26. As a member, I want a notification when my organisation is scheduled for deletion, so that I
    can save anything I need or object.
27. As a member, I want a banner with the deletion date while deletion is scheduled, so that
    nobody is surprised.
28. As an organisation's creator, I want to cancel a scheduled deletion during the grace period,
    with members told it was cancelled, so that a change of heart is easy.
29. As a member, I want the organisation to keep working normally during the grace period, so
    that work isn't interrupted by a deletion that may be cancelled.
30. As an operator, I want a record kept of each deleted organisation (its name, who scheduled it
    and when it went), so that I can answer questions about it later.
31. As an operator, I want a deleted organisation's store, files and roles torn down by the
    existing store teardown, so that nothing is left behind and no new teardown code is needed.
32. As a person invited to an organisation that's scheduled for deletion, I want the link to
    stop opening, so that I don't join something about to vanish.
33. As a member whose only organisation is deleted, I want to land on the welcome page, so that I
    can create or join another.

### Archiving

34. As a member, I want to archive an agent in one confirmation, so that I can stop it without
    working through a list of dependents.
35. As a member archiving an agent, I want to see what uses it (workflows, schedules and chats),
    so that I know what will stop working.
36. As a member archiving an agent, I want its data tables listed as becoming read-only once table
    ownership exists, so that the warning is ready when it matters.
37. As a member, I want runs already going to finish when I archive their agent, so that nobody's
    work is cut off mid-turn.
38. As a member, I want an archived agent's conversations, versions and history kept, so that
    nothing is lost.
39. As a member, I want an archived agent's schedules paused, so that it stops running on its
    own.
40. As a member, I want an archived agent's schedules to stay paused after it's unarchived until
    someone resumes them, so that unarchiving doesn't set off a burst of runs.
41. As a chat member, I want an archived agent shown as archived in the chat and left out of
    mention suggestions, so that I don't try to ask it for anything.
42. As a chat member who mentions an archived agent anyway, I want its card to say it's archived,
    with no model call made, so that I understand and nothing is spent.
43. As a member, I want a workflow that runs an archived agent, at any depth, refused when someone
    starts it, so that it fails clearly rather than halfway.
44. As a member, I want an archived agent's name kept reserved, so that unarchiving it never
    clashes with a newer agent.
45. As a member, I want to unarchive an agent and have it work as before, so that archiving is
    never a dead end.
46. As a member, I want to archive a workflow on the same terms: nothing new starts, what's going
    finishes, its schedules pause, and a workflow nesting it refuses to start.
47. As a member, I want to archive a skill even while agents use it, so that I can retire it
    without editing every agent.
48. As a member, I want agents using an archived skill to keep running on the version they pin,
    so that archiving a skill breaks nothing.
49. As an editor, I want to be stopped from adding an archived skill to a new agent version, and
    to see that a skill my agent uses is archived, so that I know to replace it.
50. As a member, I want an Archived filter on the Agents, Skills and Workflows lists, so that I
    can find archived items, and the default list to hide them.
51. As a member, I want agents deleted before this change to stay hidden, so that the lists don't
    fill with items that can't be restored.
52. As a member, I want to keep deleting individual runs, so that I can still tidy up one
    conversation without archiving its agent.

### Docs

53. As a developer, I want a short README with an overview, the architecture and a quick start,
    so that I can orient myself in minutes.
54. As a developer, I want one docs page per domain (accounts and organisations, stores, agents
    and skills, tools, workflows, chats, triggers, development, deploy, tracing, testing), so that
    I can find how one thing works without reading everything.
55. As a developer, I want every section of the old README to have exactly one home, so that
    nothing is lost or duplicated.
56. As a developer, I want the stale lines fixed as they move (stores "one per user", the plan "on
    the plan branch", "later a mention in a chat", "once runs can pause"), so that the docs match
    the code.
57. As a developer, I want the docs to use the glossary's words, so that docs, screens and
    conversation agree.

### Modules

58. As a developer, I want the models grouped by domain (identity and organisations, the store
    registry, agents and skills, workflows, triggers, approvals, notifications, chats), so that I
    can see what phase 1 moves into the store.
59. As a developer, I want the agents, chats, workflows and schema change modules split into
    smaller ones by sub-domain, so that each change touches a small file.
60. As a developer, I want every existing import and task name to keep working after the split, so
    that the move breaks nothing.
61. As a developer, I want the split to change no behaviour, schema, route or setting, so that it
    can be reviewed as a move.

### Full-stack test

62. As a developer, I want a browser test that runs the core loop (mention, schema change,
    approval, resume, reply) against the real API, workers, database, Redis and storage, so that
    a break anywhere in the loop is caught.
63. As a developer, I want the full-stack test to use a scripted model, with no calls to model,
    embedding or tracing providers, so that it's deterministic and free.
64. As an operator, I want the scripted model impossible to switch on in a production-like
    configuration, so that a test setting can't reach real users.
65. As a developer, I want later steps to add scenarios to this test rather than build new
    harnesses, so that there's one place to look.
66. As a developer, I want the stack's logs and the test report kept when it fails in CI, so that
    I can see why.

### Sharded test runs

67. As a developer, I want the backend tests split across several CI machines, each with its own
    database, Redis and storage, so that CI time stays roughly flat as tests are added.
68. As a developer, I want shards balanced on recorded test durations, refreshed on a schedule,
    so that no shard drags.
69. As a developer, I want new test files run even before their durations are recorded, so that
    nothing is skipped.
70. As a developer, I want lint, formatting and the migration check run once rather than in every
    shard, so that shards only run tests.
71. As a developer, I want one required check that passes only if every shard passed, so that
    branch protection stays simple.
72. As a developer, I want tests run in random order with the seed printed, so that tests that
    depend on each other are caught as soon as they're written.
73. As a developer, I want browser tests sharded across machines and their reports merged into
    one, so that a failure is easy to read.
74. As a developer, I want to run any single shard locally with one command, and reproduce a
    random order from its seed, so that CI failures are easy to chase.
75. As a developer, I want each shard count to be a single setting, so that adding capacity is a
    one-line change.

## Implementation Decisions

### Docs (0.1)
- The README keeps:
  - the product summary;
  - the architecture diagram and stack table;
  - a quick start: run everything in Docker, local development;
  - links to the glossary, the docs index, the decision records and the roadmap.

  Target: about 150 lines.
- Docs pages, one per domain:
  - accounts and organisations
  - stores: roles, provisioning, quotas, files, and the run history export until phase 1
    removes it
  - agents and skills
  - tools
  - workflows
  - chats
  - triggers
  - development: prerequisites, debugging, dependency policy, changing models, image size notes
  - deploy, with Fargate sandboxes
  - tracing
  - testing, written in 0.4

  There's also an index page. The development page isn't named in the plan; it's added here to
  give setup material a home.
- Pages describe today's behaviour. Later steps update their own page.

### Modules (0.2)
- **Models** become a package with one module per domain:
  - identity and organisations: users, organisations, memberships, invitations;
  - the store registry, with the run history export's outbox until phase 1 removes it;
  - agents and skills, with their versions, runs and sandbox sessions;
  - workflows, with their versions, nodes, edges, runs and steps;
  - triggers;
  - approvals: schema change requests, which keep their class name until phase 3;
  - notifications;
  - chats: chats, chat members, messages and invocations.

  The package re-exports every name, and the shared declarative base still sees every table, so
  the migration check is unaffected.
- **API modules** become packages, each still mounting one router:
  - agents: CRUD, versions, runs, and the run's chat stream;
  - chats: CRUD, members, messages and invocations.
- **Workflow execution** splits into validation, execution and steps.
- **Schema changes** split into spec, compile, apply and expiry.
- Public names, background task names and import paths used by tests are preserved.
- No module should exceed about 600 lines unless it's one cohesive unit, and the change says
  why.

### Full-stack test (0.3)
- **Compose profile.** A new profile brings up everything a chat turn and an approval need:
  - the database and roles, Redis, S3-compatible storage and its bucket;
  - platform and store migrations;
  - the API, the ingest worker, the workflow worker and the provisioning worker;
  - the web front end.

  No outbound calls.
- **Scripted model provider.** Today the scripted model is patched in-process inside tests, and a
  running server can't use it. A new model provider, `scripted:<scenario>`, is registered only
  when a new setting, **scripted models enabled** (default off), is on. Both the deployment's
  model and the handoff model can name it.
  - Scenarios live in a testing package inside the application, built on the same fake chat model
    the backend tests use. That model moves there, and the tests import it from its new home.
  - Embeddings use a deterministic fake under the same setting.
  - The API refuses to start with scripted models enabled while secure cookies are on.
- **Test runner.** The browser tests get a second configuration, pointed at the stack's web
  service, with no fake API.
- **First scenario: the core loop.**
  1. Register, then create an organisation (on the welcome page, once 0.6 lands).
  2. Open a chat and mention an agent.
  3. The agent requests a `notes` table.
  4. The card shows the run waiting.
  5. Approve on the Data page.
  6. The run resumes, writes a row, and replies "Saved."
  7. The reply appears in the chat.
- **CI.** A new job runs the stack and the suite, and keeps the report and the stack's logs on
  failure.

### Sharding (0.4)
- **Backend.** A matrix of N jobs, default 4, each with its own Postgres, Redis and storage
  services. Each job sets up roles, migrates, and runs its group of tests.
  - Groups come from pytest-split, balanced on a committed durations file.
  - A scheduled workflow, which can also be run by hand, refreshes the file and proposes it as a
    pull request.
  - New dependencies are pinned exactly, per the dependency policy.
- **Checks job.** Lint, formatting and the migration check run once, in their own job.
- **Gate jobs.** One per suite, depending on every shard. Branch protection requires the checks
  job, the backend gate, the frontend gate, the full-stack job and the Terraform job.
- **Random order.** pytest-randomly runs by default with its seed printed. Every test that fails
  under random order is fixed before sharding lands: tests relying on another test's rows, or
  counting all users, organisations or stores.
- **Frontend.** The build and unit tests run once. Playwright shards run across a matrix
  (default 3), each writing a blob report. A gate job merges them into one HTML report and
  uploads it on failure.
- **Full-stack.** Sharded the same way (default 1 until there are more scenarios), each shard
  with its own stack.
- **Configuration.** Shard counts are workflow variables, one line each.
- **Out of scope.** Parallelism inside one machine.

### Archive (0.5)
- **Schema.** Platform, expand: agents, skills and workflows gain an archived timestamp and the
  person who archived them, a nullable user reference that clears if the user is deleted.
  - Archived rows are not soft-deleted, so the existing per-organisation name uniqueness keeps
    their names reserved.
  - The soft-delete columns stay, only for rows deleted before this release. Nothing writes them
    again. Phase 1 contracts them when these tables move to the store.
- **One runnability check covers every start path:**
  - a turn started by hand;
  - a schedule firing;
  - a mention: the invocation fails at once with "The agent <name> is archived", before the
    handoff;
  - a workflow run whose pinned graph includes an archived agent or workflow at any depth. This
    extends the existing check for deleted agents.

  Turns and runs already going are not checked again.
- **Archiving an agent or workflow** disables its schedules. Unarchiving leaves them disabled.
- **Archiving a skill** blocks it from new agent versions. Pinned versions keep running, and an
  agent whose current version uses it shows a notice.
- **Removed:**
  - the agent delete endpoint, its body and the 409 dependents protocol;
  - the dependents endpoint;
  - the cancellation and deactivation half of the dependents module;
  - the skill and workflow delete endpoints;
  - the agent delete dialog.

  The finding half of the dependents module becomes the **used by** query. Run deletion stays.
- **API**, scoped to the organisation (see 0.6):

  | Endpoint | Behaviour |
  | --- | --- |
  | archive, for each kind | returns the item with its archived fields |
  | unarchive, for each kind | 409 only if a row from before this release holds the name |
  | used by, for an agent | workflows (at any depth), schedules, chats and tables (empty until phase 3) |
  | used by, for a workflow | nesting workflows, schedules and chats |
  | used by, for a skill | agents whose current version uses it |

  List endpoints take an archived filter, and every item carries its archived fields. Starting,
  mentioning or scheduling an archived item answers 409 "archived".
- **Who may archive.** Any member, until roles arrive in phase 1, matching who may delete today.
  After that, admins.

### Many organisations (0.6)
- **Schema.** Platform, expand:
  - drop the one-membership-per-user index;
  - add a unique slug to organisations, backfilled from names with numeric suffixes on clashes,
    and never changed by a rename;
  - add deletion scheduling to organisations: when it was scheduled, by whom, and when it's due;
  - add a deletion record table: organisation id, name, slug, who scheduled it, when, and when it
    was deleted, with no foreign keys so it outlives the organisation.

  A following release contracts: drop global uniqueness of organisation names, and drop the
  personal flag.
- **Sessions.** Signing in no longer ensures a membership or creates anything. The "me" response
  lists the person's organisations: id, name, slug, whether they're its creator, and any
  scheduled deletion date. It no longer names a single organisation.
- **Creating.** A new endpoint creates an organisation from a name, with the person as creator
  and only member, and provisions its store the way sign-up does today. The personal-name and
  personal-organisation logic is removed.
- **Invitations.** Accepting adds a membership. It's idempotent, answering with the
  organisation if the person is already a member. The empty-organisation refusal, the contents
  listing and the deletion of the person's personal organisation are removed. An invitation to an
  organisation scheduled for deletion no longer opens.
- **Leaving.** Removes the membership and, as today, the person's chat memberships and their
  unread schema change notifications there, handing over chats they owned. It creates nothing.
  - Refused for the last member: "Delete the organisation instead".
  - Refused for the creator while others remain. There's no transfer before roles exist.
- **Request scoping.**
  - Every organisation-scoped API route moves under an organisation prefix with the
    organisation's id: every route that today resolves the current organisation or its store.
  - The current-organisation dependency reads the id from the path and checks membership. It
    answers 404 alike for an organisation the person isn't in and one that doesn't exist. It
    remains the only place this is resolved.
  - These stay unscoped:
    - sign-in and the development sign-in picker;
    - the person's own profile;
    - opening and accepting invitations;
    - notifications, each item now carrying its organisation's id and slug;
    - the tool library;
    - the list of timezones.
- **Transition.** For one release the old unscoped routes still answer, resolving to the
  person's most recently joined membership, so an old front-end bundle keeps working during a
  rolling deploy. They're removed in the next release.
- **Deleting an organisation.**
  - Scheduling takes the typed name and is for the creator (admins after phase 1). It answers 409
    if already scheduled.
  - Every member gets a new notification kind, "organisation deletion scheduled".
  - Cancelling clears the schedule and notifies members again.
  - A new beat task on the provisioning worker deletes due organisations, every
    **organisation deletion check interval** (default 10 minutes). In one transaction it:
    1. writes the deletion record;
    2. detaches the store and marks it deleting, as accepting an invitation does today;
    3. deletes the organisation, whose platform content cascades.

    The existing teardown sweep then drops the store.
- **Front end.**
  - Every signed-in route moves under `/o/<slug>/`.
  - New routes: the welcome page, and "not found" for an organisation the person isn't in.
  - The root, and any old unprefixed path, redirects to the same path in the most recently used
    organisation. That's remembered in browser storage, with every access guarded, and falls back
    to the most recently joined organisation.
  - The shell's organisation name becomes the switcher.
  - The organisation page's Settings tab gains deletion scheduling and cancelling. Leave follows
    the new rules.
  - The invite page loses its "not empty" path.
- **Settings**, all new:

  | Setting | Default |
  | --- | --- |
  | scripted models enabled | off |
  | organisation deletion grace days | 14 |
  | organisation deletion check interval | 600 seconds |
  | backend, frontend and full-stack shard counts | 4, 3 and 1 |

### Rollout
Releases in order, each deployable alone:
1. Docs.
2. Module splits, one at a time.
3. The full-stack test and the scripted provider.
4. Sharding.
5. Archive, with its expand migration. The delete routes go in the same release, because the
   single-page app ships with the API and has no other clients.
6. Many organisations, with its expand migration and slug backfill. The API ships both route
   sets, and the web deploys after it.
7. Removal of the old routes, and the contract migration.

Rolling back release 6 is only safe before anyone holds two memberships. The release notes carry
a script that keeps each person's most recent membership for that case.

## Testing Decisions

**What makes a good test here.** It asserts what a person or another system can observe: what an
endpoint answers, what a page shows, what runs or doesn't. It doesn't assert how the code
achieves it.
- Archiving is tested by trying every way of starting something and seeing it refused, not by
  inspecting a flag.
- Organisation scoping is tested by requesting another organisation's id and getting a 404.
- The module split is tested by the existing suite passing unchanged.

**Seams, highest first:**
1. **The HTTP API**, called as a signed-in person through the test client, against real
   Postgres, Redis and S3-compatible storage, with the scripted model standing in for the
   provider. This is the seam for archive and for many organisations: memberships, scoping,
   invitations, leaving, deletion scheduling and cancellation. Prior art: most of the backend
   suite, including the organisation, invitation and chat invocation tests.
2. **The full-stack browser test**, introduced by this phase. It covers the core loop, and step
   0.6 updates it to create its organisation on the welcome page.
3. **Fake-API browser tests** for screens: the switcher, the welcome page, archive dialogs and
   filters, the deletion banner and settings, and the invite page without its "not empty" path.
   Prior art: the existing Playwright specs and their in-memory fake API, which moves its routes
   under the organisation prefix.

**Narrower seams, only where the API can't reach:**
- the provisioning worker's due-organisation task, called directly once, then the existing store
  teardown (prior art: the provisioning and store deletion tests);
- CI configuration, checked by running it: a deliberately failing shard must fail the gate and
  leave a downloadable report.

**What's tested, by step:**
- **0.2:** the whole suite, unchanged.
- **0.4:** the whole suite under random order.
- **0.5:**
  - every start path refused for archived agents and workflows (by hand, schedule, mention,
    nested workflow), with no model call on a refused mention;
  - runs already going finish;
  - schedules stay paused across unarchiving;
  - names stay reserved;
  - rows deleted before the release stay hidden;
  - used-by results.

  The dependents tests and the agent delete browser spec are removed.
- **0.6:**
  - two memberships;
  - every organisation-scoped route refusing another organisation's id, as one test generated
    from the router, so new routes are covered automatically;
  - idempotent accepts;
  - leave refusals;
  - slugs and clashes;
  - deletion scheduling, cancellation and completion through store teardown;
  - the transition routes.

  The empty-organisation tests are removed, and the organisation, invitation, scoping and auth
  tests are rewritten for no personal organisations.
- **Adversarial:**
  - scoping as above;
  - the API refusing to start with scripted models enabled and secure cookies on.

## Out of Scope

- **Roles** (admin, editor, participant) and transferring an organisation's creator. Phase 1.
- **Moving content into the store**, including contracting the soft-delete columns. Phase 1.
- **Chat changes:** owners, private chats, archiving chats, direct messages. Phase 2. Leaving
  still hands over owned chats.
- **Table ownership.** Phase 3. The archive dialog's table list is empty until then.
- **Parallel tests on one machine.**
- **Renaming tables, tool keys or the store tree.**

## Further Notes

**What this hands to phase 1**
- Phase 1 ([phase-1-foundation.md](phase-1-foundation.md)) builds on:
  - the domain-grouped models (the store move goes module by module);
  - the organisation prefix on every route (roles hang off memberships);
  - the full-stack test and sharded CI.
- Roles replace "creator" wherever this phase says only the creator may act. Phase 1 decides who
  may archive.

**Risks**
- **Moving every route under a prefix** touches the whole API and every browser spec at once.
  The generated scoping test and the one-release transition routes contain the risk.
- **Rolling back many organisations** is only clean before people hold several memberships.
- **Random order may expose many order-dependent tests.** Budget time to fix them before
  sharding lands.

**Open questions** (new, raised by this spec)
1. **Organisation names.** This spec drops their global uniqueness in favour of unique slugs.
   Confirm.
2. **The gap before roles.** A creator can't leave an organisation others are in until phase 1
   adds admins. Accept the gap, or add a small "transfer creator" action now?
3. **Slugs or ids in links.** This spec uses slugs in browser links and ids in the API. Confirm.
4. **Who may archive before roles.** Any member, or only the creator?
