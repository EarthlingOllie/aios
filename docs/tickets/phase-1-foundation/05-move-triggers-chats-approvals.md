# 05: Triggers, chats, invocations and schema change requests move

**What to build:** Schedules, chats, chat members, messages, mentions, invocations and schema change requests move into the store in one change per domain that also deletes the old platform tables; dev data is rebuilt with `make seed`. Schedule definitions live in the store while the platform keeps only a thin index of what's due, written when a schedule is saved and rebuilt by the repair sweep from the store's triggers; firing re-reads the schedule under a row lock. Chats move as they are, owner and visibility columns included; phase 2's open chats (P2-01) drop them. Chat invocations become the store's general invocations table, with mention as their cause. Notifications keep pointing at the right organisation's approvals.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [04: Runs, checkpoints and workflow runs move](04-move-runs-and-checkpoints.md)

**Status:** done

- [x] Editing a schedule updates its due-index row, a lost due row is rebuilt by the repair sweep (which reports it), and an edit racing a claim drops that firing.
  (Store revision 0012 makes `triggers` in every store; the platform keeps `schedule_due`
  (store, trigger, organisation, next run time), written once the store has committed a save,
  pause, archive or delete (`aios.schedules.index`), and rebuilt from every ready store's
  triggers by the repair sweep (`schedules.repair`, run by `aios.repair_stores` with the people
  mirror's), which logs and returns what it added, updated and removed. Claiming locks the due
  rows (`SKIP LOCKED`), re-reads each trigger in its store under its row lock (skipping one an
  edit or a starting firing holds), moves it on there, then its row; firing re-reads it under
  the lock and drops a firing an edit cancelled.
  `test_schedules_chats_requests_in_store.py` (saving writes the row, the repair sweep and its
  task, a row ahead of its trigger, an edit racing a claim, the same id in two stores) and
  `test_scheduler.py`.)
- [x] The Scheduler page and each agent's and workflow's schedules work from the store.
  (`/api/triggers` reads and writes the store, its shape unchanged; `test_triggers.py`, the
  archive tests and the browser tests' `schedules.spec.ts` pass.)
- [x] Posting, mentions, run cards, the handoff and unread counts work from the store.
  (`chats`, `chat_members`, `chat_messages` and `invocations` (chat invocations became the
  store's general invocations, with a `cause` that's `mention` for now) are in the store, owner
  and visibility as they were; invocations name runs, and are named, by store and id, and a run
  deleted clears its invocation's link by foreign key. Leaving an organisation takes the leaver
  out of its chats in a store transaction that commits as the leave does. `test_chats.py`,
  `test_chat_*.py`, `test_firing.py`, and the full-stack core loop.)
- [x] Approving, applying, expiring and cancelling schema change requests work from the store, and the chat or workflow run that asked resumes.
  (`schema_change_requests` is in the store, beside the runs it names by foreign key; worker
  tasks carry the store (`apply_schema_change_task(data_store_id, request_id)`); the decision
  commits in the store first, then its notifications are cleared as of it (undone if they
  can't be). `test_schema_change_*.py`, `test_workflow_pause.py`, `test_dropped.py`,
  `test_notifications.py`.)
- [x] A notification opens the right organisation's approval.
  (Notifications name the request by its id and store, and say their organisation; titles are
  read from each request's store.
  `test_schedules_chats_requests_in_store.py::test_a_notification_opens_the_right_organisations_approval`.)
- [x] The platform database holds no organisation content: only identity, organisations, memberships, invitations, the store registry and its credentials, notifications, and the schedule due index. The full-stack test passes.
  (Platform revision 0063 drops `triggers`, `chats`, `chat_members`, `chat_messages`,
  `chat_invocations` and `schema_change_requests`, and adds `schedule_due`; its downgrade brings
  them back empty, the schema identical to 0062's. What's left: users, organisations, their
  members, invitations and deletions, `data_stores` (with the run history export's outbox,
  `history_exports`), notifications and `schedule_due`.
  `test_schedules_chats_requests_in_store.py::test_the_platform_holds_no_organisation_content_and_the_store_holds_what_moved`;
  the full-stack core loop passes.)
