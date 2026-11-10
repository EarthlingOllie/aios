# Tickets

The platform cohesion work, broken into tickets: thin slices that each work end to end, with
the tickets that block them. They come from the phase specs in [`docs/specs/`](../specs/), which
follow [decision record 0001](../decisions/0001-organisation-database-as-system-of-record.md)
and [the plan](../../plan/platform-cohesion/plan.md). Words are the glossary's
([`CONTEXT.md`](../../CONTEXT.md)).

Work the frontier: any ticket whose blockers are all done can start. `P1-05` means phase 1,
ticket 05. Each ticket's status line says whether it's `ready-for-agent`, in progress or done.

## Phase 0: Refactor and guard rails

Spec: [phase-0-refactor-and-guard-rails.md](../specs/phase-0-refactor-and-guard-rails.md)

| # | Ticket | Blocked by |
| --- | --- | --- |
| 01 | [Docs by domain, and a slim README](phase-0-refactor-and-guard-rails/01-docs-by-domain.md) | — |
| 02 | [Models split into a package by domain](phase-0-refactor-and-guard-rails/02-models-package.md) | — |
| 03 | [Split the largest modules](phase-0-refactor-and-guard-rails/03-split-large-modules.md) | — |
| 04 | [Full-stack test, with the scripted model as a selectable provider](phase-0-refactor-and-guard-rails/04-full-stack-test.md) | — |
| 05 | [Backend suite sharded, in random order, with one checks job](phase-0-refactor-and-guard-rails/05-sharded-backend-suite.md) | — |
| 06 | [Browser and full-stack tests sharded](phase-0-refactor-and-guard-rails/06-sharded-browser-and-full-stack-tests.md) | [04](phase-0-refactor-and-guard-rails/04-full-stack-test.md) |
| 07 | [Archive agents](phase-0-refactor-and-guard-rails/07-archive-agents.md) | — |
| 08 | [Archive workflows and skills; remove every hard delete](phase-0-refactor-and-guard-rails/08-archive-workflows-and-skills.md) | [07](phase-0-refactor-and-guard-rails/07-archive-agents.md) |
| 09 | [Organisation in the path](phase-0-refactor-and-guard-rails/09-organisation-in-the-path.md) | [02](phase-0-refactor-and-guard-rails/02-models-package.md) |
| 10 | [Move every route and screen under the organisation (notifications get their organisation here)](phase-0-refactor-and-guard-rails/10-move-routes-under-organisation.md) | [09](phase-0-refactor-and-guard-rails/09-organisation-in-the-path.md) |
| 11 | [Many organisations: signing up makes nothing, join, switcher, leave](phase-0-refactor-and-guard-rails/11-many-organisations.md) | [04](phase-0-refactor-and-guard-rails/04-full-stack-test.md), [10](phase-0-refactor-and-guard-rails/10-move-routes-under-organisation.md) |
| 12 | [Delete an organisation with a grace period; remove unscoped routes](phase-0-refactor-and-guard-rails/12-delete-organisation.md) | [11](phase-0-refactor-and-guard-rails/11-many-organisations.md) |

## Phase 1: Foundation

Spec: [phase-1-foundation.md](../specs/phase-1-foundation.md)

| # | Ticket | Blocked by |
| --- | --- | --- |
| 01 | [Connection pooler and turn lease](phase-1-foundation/01-pooler-and-turn-lease.md) | [P0-02](phase-0-refactor-and-guard-rails/02-models-package.md), [P0-05](phase-0-refactor-and-guard-rails/05-sharded-backend-suite.md), [P0-07](phase-0-refactor-and-guard-rails/07-archive-agents.md) |
| 02 | [People mirror](phase-1-foundation/02-people-mirror.md) | [P0-02](phase-0-refactor-and-guard-rails/02-models-package.md), [P0-12](phase-0-refactor-and-guard-rails/12-delete-organisation.md) |
| 03 | [Agents, skills and workflows move to the store](phase-1-foundation/03-move-agents-skills-workflows.md) | [02](phase-1-foundation/02-people-mirror.md), [P0-08](phase-0-refactor-and-guard-rails/08-archive-workflows-and-skills.md) |
| 04 | [Runs, checkpoints and workflow runs move](phase-1-foundation/04-move-runs-and-checkpoints.md) | [01](phase-1-foundation/01-pooler-and-turn-lease.md), [03](phase-1-foundation/03-move-agents-skills-workflows.md) |
| 05 | [Triggers, chats, invocations and schema change requests move](phase-1-foundation/05-move-triggers-chats-approvals.md) | [04](phase-1-foundation/04-move-runs-and-checkpoints.md) |
| 06 | [Unified search across documents, chats and runs](phase-1-foundation/06-unified-search.md) | [05](phase-1-foundation/05-move-triggers-chats-approvals.md) |
| 07 | [Roles and permissions on every route](phase-1-foundation/07-roles-and-permissions.md) | [02](phase-1-foundation/02-people-mirror.md), [P0-12](phase-0-refactor-and-guard-rails/12-delete-organisation.md) |
| 08 | [Audit log and approval rules](phase-1-foundation/08-audit-log-and-approval-rules.md) | [05](phase-1-foundation/05-move-triggers-chats-approvals.md), [07](phase-1-foundation/07-roles-and-permissions.md) |
| 09 | [Retention for runs and chats](phase-1-foundation/09-retention.md) | [06](phase-1-foundation/06-unified-search.md), [08](phase-1-foundation/08-audit-log-and-approval-rules.md), [10](phase-1-foundation/10-invocations-and-turns.md) |
| 10 | [An invocation for every run, and turns as records](phase-1-foundation/10-invocations-and-turns.md) | [05](phase-1-foundation/05-move-triggers-chats-approvals.md) |
| 11 | [Usage ledger in GBP, caps, mention guards](phase-1-foundation/11-usage-caps-and-mention-guards.md) | [06](phase-1-foundation/06-unified-search.md), [07](phase-1-foundation/07-roles-and-permissions.md), [10](phase-1-foundation/10-invocations-and-turns.md) |
| 12 | [Run service: queue, mentioned turns, handoff, scheduler](phase-1-foundation/12-run-service.md) | [01](phase-1-foundation/01-pooler-and-turn-lease.md), [10](phase-1-foundation/10-invocations-and-turns.md) |
| 13 | [Workflows node by node; approvals resume there; workflow worker removed](phase-1-foundation/13-workflows-node-by-node.md) | [12](phase-1-foundation/12-run-service.md) |
| 14 | [Everything on the run service: graceful shutdown, rate limiter, embeddings, run pages read-only, API turn path removed](phase-1-foundation/14-everything-on-run-service.md) | [06](phase-1-foundation/06-unified-search.md), [12](phase-1-foundation/12-run-service.md), [13](phase-1-foundation/13-workflows-node-by-node.md), [08](phase-1-foundation/08-audit-log-and-approval-rules.md), [11](phase-1-foundation/11-usage-caps-and-mention-guards.md) |
| 15 | [Jev: classify untrusted content](phase-1-foundation/15-jev-classification.md) | [06](phase-1-foundation/06-unified-search.md), [08](phase-1-foundation/08-audit-log-and-approval-rules.md) |

## Phase 2: Organisation experience

Spec: [phase-2-organisation-experience.md](../specs/phase-2-organisation-experience.md)

| # | Ticket | Blocked by |
| --- | --- | --- |
| 01 | [Open chats, and the Jev check on posted messages](phase-2-organisation-experience/01-open-chats.md) | [P1-05](phase-1-foundation/05-move-triggers-chats-approvals.md), [P1-10](phase-1-foundation/10-invocations-and-turns.md), [P1-15](phase-1-foundation/15-jev-classification.md) |
| 02 | [Chats without owners, and archiving chats](phase-2-organisation-experience/02-chats-without-owners.md) | [01](phase-2-organisation-experience/01-open-chats.md) |
| 04 | [Streamed conversations and chat list, with limits and fallback](phase-2-organisation-experience/04-streaming.md) | [01](phase-2-organisation-experience/01-open-chats.md), [02](phase-2-organisation-experience/02-chats-without-owners.md), [P1-12](phase-1-foundation/12-run-service.md) |
| 05 | [One chat experience: "Chat with this agent"](phase-2-organisation-experience/05-one-chat-experience.md) | [02](phase-2-organisation-experience/02-chats-without-owners.md), [04](phase-2-organisation-experience/04-streaming.md), [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md) |
| 06 | [Organisation assistant and default chat agents](phase-2-organisation-experience/06-organisation-assistant.md) | [05](phase-2-organisation-experience/05-one-chat-experience.md), [P1-06](phase-1-foundation/06-unified-search.md) |
| 07 | [Notifications across organisations: mentions, replies, failures](phase-2-organisation-experience/07-notifications.md) | [04](phase-2-organisation-experience/04-streaming.md) |
| 08 | [Handback, and delivery to chats](phase-2-organisation-experience/08-handback-and-delivery.md) | [02](phase-2-organisation-experience/02-chats-without-owners.md), [07](phase-2-organisation-experience/07-notifications.md), [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md), [P1-13](phase-1-foundation/13-workflows-node-by-node.md) |
| 09 | [post_message tool](phase-2-organisation-experience/09-post-message.md) | [02](phase-2-organisation-experience/02-chats-without-owners.md), [07](phase-2-organisation-experience/07-notifications.md), [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md) |
| 10 | [Export and import agents and workflows](phase-2-organisation-experience/10-export-and-import.md) | [P1-03](phase-1-foundation/03-move-agents-skills-workflows.md), [P1-08](phase-1-foundation/08-audit-log-and-approval-rules.md) |
| 11 | [Phase 2 full-stack scenarios](phase-2-organisation-experience/11-full-stack-and-contracts.md) | [05](phase-2-organisation-experience/05-one-chat-experience.md), [08](phase-2-organisation-experience/08-handback-and-delivery.md), [09](phase-2-organisation-experience/09-post-message.md) |

## Phase 3: Agents and automation

Spec: [phase-3-agents-and-automation.md](../specs/phase-3-agents-and-automation.md)

| # | Ticket | Blocked by |
| --- | --- | --- |
| 01 | [Continuing runs on the current version, with queued mentions](phase-3-agents-and-automation/01-continuing-runs.md) | [P1-12](phase-1-foundation/12-run-service.md), [P2-01](phase-2-organisation-experience/01-open-chats.md), [P2-05](phase-2-organisation-experience/05-one-chat-experience.md), [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md), [P2-08](phase-2-organisation-experience/08-handback-and-delivery.md) |
| 02 | [Compaction of long conversations](phase-3-agents-and-automation/02-compaction.md) | [01](phase-3-agents-and-automation/01-continuing-runs.md), [P1-09](phase-1-foundation/09-retention.md), [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md), [P1-15](phase-1-foundation/15-jev-classification.md) |
| 03 | [Memory: tools, recall, Memory tab](phase-3-agents-and-automation/03-memory.md) | [P1-06](phase-1-foundation/06-unified-search.md), [P1-08](phase-1-foundation/08-audit-log-and-approval-rules.md), [P1-15](phase-1-foundation/15-jev-classification.md), [P2-10](phase-2-organisation-experience/10-export-and-import.md) |
| 04 | [Table ownership, sharing and transfer](phase-3-agents-and-automation/04-table-ownership.md) | [P0-08](phase-0-refactor-and-guard-rails/08-archive-workflows-and-skills.md), [P1-05](phase-1-foundation/05-move-triggers-chats-approvals.md), [P1-07](phase-1-foundation/07-roles-and-permissions.md), [P1-08](phase-1-foundation/08-audit-log-and-approval-rules.md) |
| 05 | [Per-agent database logins, with the escape test](phase-3-agents-and-automation/05-per-agent-database-logins.md) | [04](phase-3-agents-and-automation/04-table-ownership.md), [P1-01](phase-1-foundation/01-pooler-and-turn-lease.md), [13](phase-3-agents-and-automation/13-lanes-and-autoscaling.md) |
| 06 | [Structured workflows: typed I/O, templates, conditional edges](phase-3-agents-and-automation/06-structured-workflows.md) | [P1-13](phase-1-foundation/13-workflows-node-by-node.md), [P1-15](phase-1-foundation/15-jev-classification.md) |
| 07 | [Approvals as one concept, and the approval node](phase-3-agents-and-automation/07-approvals-and-approval-node.md) | [06](phase-3-agents-and-automation/06-structured-workflows.md), [P2-07](phase-2-organisation-experience/07-notifications.md) |
| 08 | [start_workflow tool](phase-3-agents-and-automation/08-start-workflow.md) | [06](phase-3-agents-and-automation/06-structured-workflows.md), [P2-08](phase-2-organisation-experience/08-handback-and-delivery.md) |
| 09 | [Remove old forms: approvals rename; read-write role loses data writes](phase-3-agents-and-automation/09-approvals-rename-and-write-contract.md) | [05](phase-3-agents-and-automation/05-per-agent-database-logins.md), [07](phase-3-agents-and-automation/07-approvals-and-approval-node.md) |
| 10 | [Budgets and allowances in GBP](phase-3-agents-and-automation/10-budgets-and-allowances.md) | [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md), [08](phase-3-agents-and-automation/08-start-workflow.md), [12](phase-3-agents-and-automation/12-more-events.md), [P2-07](phase-2-organisation-experience/07-notifications.md) |
| 11 | [Event triggers: outbox, dispatcher, loop prevention, document ingested](phase-3-agents-and-automation/11-event-triggers.md) | [P1-10](phase-1-foundation/10-invocations-and-turns.md), [P1-12](phase-1-foundation/12-run-service.md), [P1-08](phase-1-foundation/08-audit-log-and-approval-rules.md) |
| 12 | [More events: table written, schema applied, chat message, debounce, webhooks](phase-3-agents-and-automation/12-more-events.md) | [06](phase-3-agents-and-automation/06-structured-workflows.md), [11](phase-3-agents-and-automation/11-event-triggers.md), [P2-08](phase-2-organisation-experience/08-handback-and-delivery.md), [P2-09](phase-2-organisation-experience/09-post-message.md), [P1-15](phase-1-foundation/15-jev-classification.md) |
| 13 | [Lanes and autoscaling](phase-3-agents-and-automation/13-lanes-and-autoscaling.md) | [P1-12](phase-1-foundation/12-run-service.md) |

## Phase 4: Inflow and insight

Spec: [phase-4-inflow-and-insight.md](../specs/phase-4-inflow-and-insight.md)

| # | Ticket | Blocked by |
| --- | --- | --- |
| 01 | [Sources foundation: organisation-aware tool checks, Uploads source](phase-4-inflow-and-insight/01-sources-foundation.md) | [P0-12](phase-0-refactor-and-guard-rails/12-delete-organisation.md), [P1-03](phase-1-foundation/03-move-agents-skills-workflows.md) |
| 02 | [Sync engine, file items, source health](phase-4-inflow-and-insight/02-sync-engine.md) | [01](phase-4-inflow-and-insight/01-sources-foundation.md), [P1-15](phase-1-foundation/15-jev-classification.md), [P1-12](phase-1-foundation/12-run-service.md), [P2-07](phase-2-organisation-experience/07-notifications.md), [P3-13](phase-3-agents-and-automation/13-lanes-and-autoscaling.md) |
| 03 | [URL, webhook and email sources](phase-4-inflow-and-insight/03-url-webhook-email-sources.md) | [02](phase-4-inflow-and-insight/02-sync-engine.md), [P3-12](phase-3-agents-and-automation/12-more-events.md) |
| 04 | [Connectors: credentials, allowlist, choosing what syncs, removal](phase-4-inflow-and-insight/04-connectors.md) | [02](phase-4-inflow-and-insight/02-sync-engine.md), [P1-08](phase-1-foundation/08-audit-log-and-approval-rules.md) |
| 05 | [Connector tools and the MCP client](phase-4-inflow-and-insight/05-connector-tools-and-mcp.md) | [01](phase-4-inflow-and-insight/01-sources-foundation.md), [04](phase-4-inflow-and-insight/04-connectors.md), [P1-07](phase-1-foundation/07-roles-and-permissions.md), [P1-15](phase-1-foundation/15-jev-classification.md), [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md) |
| 06 | [GitHub](phase-4-inflow-and-insight/06-github.md) | [04](phase-4-inflow-and-insight/04-connectors.md), [05](phase-4-inflow-and-insight/05-connector-tools-and-mcp.md) |
| 07 | [Slack](phase-4-inflow-and-insight/07-slack.md) | [04](phase-4-inflow-and-insight/04-connectors.md), [05](phase-4-inflow-and-insight/05-connector-tools-and-mcp.md) |
| 08 | [Google Drive](phase-4-inflow-and-insight/08-google-drive.md) | [04](phase-4-inflow-and-insight/04-connectors.md) |
| 09 | [Insights page and every panel](phase-4-inflow-and-insight/09-insights.md) | [02](phase-4-inflow-and-insight/02-sync-engine.md), [P1-11](phase-1-foundation/11-usage-caps-and-mention-guards.md), [P3-07](phase-3-agents-and-automation/07-approvals-and-approval-node.md) |
| 10 | [Ask for a chart, and saved charts](phase-4-inflow-and-insight/10-prompted-charts.md) | [09](phase-4-inflow-and-insight/09-insights.md), [P3-10](phase-3-agents-and-automation/10-budgets-and-allowances.md) |
| 11 | [Inflow full-stack test](phase-4-inflow-and-insight/11-full-stack-and-contract.md) | [03](phase-4-inflow-and-insight/03-url-webhook-email-sources.md), [04](phase-4-inflow-and-insight/04-connectors.md), [05](phase-4-inflow-and-insight/05-connector-tools-and-mcp.md), [09](phase-4-inflow-and-insight/09-insights.md), [P2-06](phase-2-organisation-experience/06-organisation-assistant.md) |
