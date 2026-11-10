# Roadmap

What aios will need beyond the work in
[decision record 0001](decisions/0001-organisation-database-as-system-of-record.md), and why
each item waits. Terms are the glossary's, [`CONTEXT.md`](../CONTEXT.md). Add to this log as
items are deferred, and move an item into a decision record when it's taken on.

## Enterprise

### Single sign-on

Enterprise customers need to sign in through their own identity provider.

- OIDC (Okta, Entra ID, Google Workspace) and SAML.
- Verified domains that route a company's people to its identity provider.
- Optional just-in-time membership, with a default role.
- SCIM provisioning and deprovisioning, mapped onto roles, and later onto teams.

Email and password accounts stay for everyone else. Every sign-in already goes through one
place (`aios.auth.start_session`), which is where an SSO sign-in joins them.

Single sign-on also provides the identity matching that **permission-aware sync** needs (below).

### Roles and teams

0001 gives organisations three fixed roles (admin, editor, participant) and makes the
organisation the only boundary of who sees what. A company that shares one organisation (the
recommended setup) will want more than that:

- **Teams:** groups of members inside an organisation, each with its own visibility over
  documents, chats, agents, workflows, data tables, memory and synced items, while still
  sharing one store.
- **Finer role-based access control (RBAC):**
  - custom roles;
  - permissions per resource: who may use, edit or approve for a given agent, workflow, data
    table or connector;
  - roles granted per team.
- **Team mapping from the identity provider:** SCIM groups become teams.
- **Company-wide administration** for a company that runs several organisations: shared admins,
  single sign-on, billing and a catalogue of agents across them.

The hard part is visibility inside one store that agents query with SQL they write themselves.
It needs database roles or row-level security per team, building on the per-agent database
roles in 0001 (D5), rather than filters in application code.

**Until then:** a company that needs departments kept apart makes one organisation per
department and moves agents between them by export and import (0001, D14).

### Permission-aware sync

Today syncing a source shares its content with the whole organisation, and the admin is warned
when they pick something restricted. Keeping each item's permissions from its source means
matching Slack, Google and GitHub identities to people in aios, then filtering search, memory
and the organisation assistant by those permissions.

**Waits for:** single sign-on, and teams.

## Commercial

### Billing

The usage ledger (0001, D12) becomes the basis for billing:

- priced plans with included usage;
- limits set by plan: budgets, and the per-organisation in-flight cap from D13;
- metered overage, invoices and a billing provider;
- the cost estimates on Insights replaced by billed figures.

**Until then:** Insights shows estimates, and budgets are internal guards with generous,
configurable defaults.

### Real usage limits

Every limit in 0001 is configurable but set generously: per-run caps, budgets, the participant
allowance, mention rate limits and the per-organisation in-flight cap. Set real defaults once the
usage ledger has a few weeks of data.

## Product

### Value-based pruning

Beyond pruning by age, a periodic semantic scan over chats and runs would judge how much context
each holds: decisions, facts the team relies on, and things agents still refer to, as against
chatter and routine runs. It would offer admins targeted options, such as keeping a decision and
dropping the discussion around it, or summarising a thread before it's pruned. It would itself be
a budgeted run.

### Retention per chat

A chat could choose to expire sooner than the organisation's chat retention.

### Notification delivery

Email and other off-app delivery of notifications, and per-kind preferences.

### Email sources without inbound mail

Email sources need an inbound mail service. Deployments without one get them once aios provides
or integrates one.

### Templates

Publishing exported agents and workflows as templates that any organisation can import.
