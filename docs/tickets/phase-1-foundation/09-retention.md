# 09: Retention for runs and chats

**What to build:** Admins choose how long runs are kept and how long chat history is kept, covering every chat, each defaulting to keep indefinitely. A nightly, batched job prunes finished runs with their steps, turns, sandbox sessions, histories and search entries, and old messages with their mentions, invocations, briefs and search entries, keeping each chat. Work in flight is never touched, and each prune is recorded in the audit log.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [08: Audit log and approval rules](08-audit-log-and-approval-rules.md), [10: An invocation for every run, and turns as records](10-invocations-and-turns.md)

**Status:** done

- [x] Both settings appear on the Organisation page's Settings tab, editable by admins and read-only for editors and participants.
- [x] Keep indefinitely, the default, prunes nothing; switching back to it stops further pruning.
- [x] Runs that are running, queued or waiting, or waiting on an approval, are never pruned.
- [x] A message whose invocation is unfinished is kept until it finishes, and chats remain after their messages are pruned.
- [x] Documents, data tables, usage and the audit log are never pruned.
- [x] Each run of the job writes one audit entry per kind with the count and the cutoff, and deletes in batches.
