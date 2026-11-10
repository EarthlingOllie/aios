# 07: Approvals as one concept, and the approval node

**What to build:** Schema changes and workflow sign-offs become one concept: the **approval**.

- **One mechanism.** The waiting, notification, expiry, cancellation and resume behaviour is extracted from schema change requests and shared.
- **One list.** The Data page's Changes tab becomes the Approvals list, covering both kinds.
- **The approval node.** A new workflow node kind pauses the run for a person in a set role to approve or reject, and its structured outcome can drive conditional edges.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [06: Structured workflows: typed I/O, templates, conditional edges](06-structured-workflows.md), [Phase 1 · 08: Audit log and approval rules](../phase-1-foundation/08-audit-log-and-approval-rules.md), [Phase 2 · 07: Notifications across organisations: mentions, replies, failures](../phase-2-organisation-experience/07-notifications.md)

**Status:** ready-for-agent

- [ ] The existing schema change behaviour and tests pass through the new approvals API, which replaces the old schema change routes.
- [ ] The Approvals list filters by kind and status, and shows decision buttons only to people who may decide.
- [ ] Approvers see the full reason and input.
- [ ] An approval node renders its prompt, notifies only those who may decide, and waits.
- [ ] Approving or rejecting resumes the run with a structured outcome that edges can branch on. An expired approval counts as not approved. Cancelling the run cancels the approval and clears its notifications.
- [ ] Only the node's configured role may decide. Drops still need the table names typed, and every decision is audited.
- [ ] The full-stack test covers a workflow that branches on structured output, pauses at an approval node, and resumes on the decision.
