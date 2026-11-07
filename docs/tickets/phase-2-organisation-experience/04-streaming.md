# 04: Streamed conversations and chat list, with limits and fallback

**What to build:** Conversations and the chat list update live instead of polling.

- **The conversation stream.** An open conversation streams over server-sent events, fed through Redis pub/sub. It carries:
  - new messages;
  - run card updates as they happen;
  - a draft of the agent's reply, marked "writing…", which the final message replaces in place.
- **The person stream.** A per-person stream, covering every organisation the person is in, keeps the chat list's unread counts and entries live.
- **Reconnecting.** A reconnect catches up from the last event id, with no gaps and no duplicates.
- **Closing.** Leaving, being removed, archiving, or leaving the organisation closes the relevant streams with the reason.
- **Limits.** Caps per process and per person keep load bounded.
- **Fallback.** If Redis is unavailable, the app falls back to periodic reads and resumes streaming by itself.
- **No more polling.** The one-second conversation poll and the five-second chat list poll are removed.
- **Test helper.** This ticket adds a test helper that reads server-sent events streams.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [01: Visibility check for conversations; private chats become direct messages](01-visibility-and-direct-message-migration.md), [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md)

**Status:** ready-for-agent

- [ ] New messages arrive on the conversation stream without the browser polling.
- [ ] A reconnect with a last event id delivers every missed message exactly once, including a message published between subscribing and reading.
- [ ] A run card moves from queued to briefing to running to finished on the stream.
- [ ] A draft grows as the reply is written, and the final message replaces it in place. Opening a conversation mid-reply shows the draft so far.
- [ ] When a turn fails, the draft is removed and the failure is shown. Drafts never reach the database.
- [ ] Unread counts and chat list changes (appearing, left, renamed, archived) update live from the person stream. Direct messages appear only for their chat members.
- [ ] Outsiders get a 404 on a conversation's stream.
- [ ] Each of the four causes sends "gone" with its reason, and the client stops reconnecting.
- [ ] Over the per-process cap, the stream answers 503 with a retry hint. Over the per-person cap, the oldest stream is closed as replaced.
- [ ] With Redis down, streams answer 503, the client falls back to periodic reads, and recovers once Redis is back.
- [ ] Heartbeats run on the configured interval, and the proxy configurations have buffering off and timeouts above the heartbeat.
