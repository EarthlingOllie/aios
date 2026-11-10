# 14: Everything on the run service: graceful shutdown, rate limiter, embeddings, run pages read-only, API turn path removed

**What to build:** Every remaining kind of agent work runs on the run service, and the web servers run none. The run page's chat is removed: run pages become read-only, showing the run's trace, events and usage, and no run-page turn path or relay is built. Until phase 2, people talk to agents by mentioning them in chats. A deploy hands unfinished turns back to the queue to resume from their last saved point, and a process whose event loop stalls exits so its work is retried elsewhere. A shared limiter per model keeps calls within the provider's rate limits, and background embedding of the search index runs on the run service. The old turn path, the per-process cap and the old heartbeat columns are removed, and the full-stack core loop runs end to end on the run service.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [12: Run service: queue, mentioned turns, handoff, scheduler](12-run-service.md), [13: Workflows node by node; approvals resume there; workflow worker removed](13-workflows-node-by-node.md), [08: Audit log and approval rules](08-audit-log-and-approval-rules.md), [11: Usage ledger in GBP, caps, mention guards](11-usage-caps-and-mention-guards.md)

**Status:** ready-for-agent

- [ ] Run pages are read-only, showing the run's trace, events and usage; the run page's chat, its endpoint and its tests are gone.
- [ ] Full-stack scenario: restarting the run service mid-turn resumes the turn, with no "stopped mid-run" card; a graceful hand-back doesn't count as an attempt.
- [ ] A process whose loop stalls past the no-progress limit exits, and its work is retried elsewhere.
- [ ] With a model rate limit configured, calls across two processes stay within it, people waiting in chat are served first, and an unset limit means no pacing.
- [ ] Unembedded search items are embedded by background sweeps that never take capacity reserved for chat.
- [ ] Web servers run no agent turns, and the per-process concurrent-runs setting and the old heartbeat columns are gone.
- [ ] The full-stack core loop (mention, schema change, approval, resume, reply) runs on the run service through the pooler, writes one usage row per model call, needs an editor to approve, and appears in the audit log.
