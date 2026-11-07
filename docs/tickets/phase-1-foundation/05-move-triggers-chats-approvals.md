# 05: Triggers, chats, invocations and schema change requests move

**What to build:** Schedules, chats, chat members, messages, mentions, invocations and schema change requests move into the store. Schedule definitions live in the store while the platform keeps only a thin index of what's due, rebuilt by the reconciler; firing re-reads the schedule under a row lock. Chat invocations become the store's general invocations table, with mention as their cause. Notifications keep pointing at the right organisation's approvals.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [04: Runs, checkpoints and workflow runs move](04-move-runs-and-checkpoints.md)

**Status:** ready-for-agent

- [ ] Editing a schedule updates its due-index row, a lost due row is rebuilt by the reconciler, and an edit racing a claim drops that firing.
- [ ] The Scheduler page and each agent's and workflow's schedules work from the store.
- [ ] Posting, mentions, run cards, the handoff and unread counts work from the store with reads switched.
- [ ] Approving, applying, expiring and cancelling schema change requests work from the store, and the chat or workflow run that asked resumes.
- [ ] A notification opens the right organisation's approval.
- [ ] The verifier reports zero differences for every domain in this ticket.
