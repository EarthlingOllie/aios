# 14: Everything on the run service: run-page turns, graceful shutdown, rate limiter, embeddings, API turn path removed

**What to build:** Every remaining kind of agent work runs on the run service, and the web servers run none. Turns posted on the run page are relayed from the run service in today's streaming format; as decided after review, a run-page turn is still abandoned when the browser disconnects. A deploy hands unfinished turns back to the queue to resume from their last saved point, and a process whose event loop stalls exits so its work is retried elsewhere. A shared limiter per model keeps calls within the provider's rate limits, and background embedding of the search index runs on the run service. The old turn path, the per-process cap, the temporary switch and the old heartbeat columns are removed, and the full-stack core loop runs end to end on the run service.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [12: Run service: queue, mentioned turns, handoff, scheduler](12-run-service.md), [13: Workflows node by node; approvals resume there; workflow worker removed](13-workflows-node-by-node.md)

**Status:** ready-for-agent

- [ ] Run-page replies stream as before; closing the tab stops the turn and keeps what it got to; a run that stays busy past the wait answers 409.
- [ ] Full-stack scenario: restarting the run service mid-turn resumes the turn, with no "stopped mid-run" card; a graceful hand-back doesn't count as an attempt.
- [ ] A process whose loop stalls past the no-progress limit exits, and its work is retried elsewhere.
- [ ] With a model rate limit configured, calls across two processes stay within it, people waiting in chat are served first, and an unset limit means no pacing.
- [ ] Unembedded search items are embedded by background sweeps that never take capacity reserved for chat.
- [ ] Web servers run no agent turns, and the per-process concurrent-runs setting, the temporary switch and the old heartbeat columns are gone.
- [ ] The full-stack core loop (mention, schema change, approval, resume, reply) runs on the run service through the pooler, writes one usage row per model call, needs an editor to approve, and appears in the audit log.
