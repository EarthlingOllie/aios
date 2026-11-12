# 04: Streamed conversations and chat list, with limits and fallback

**What to build:** Conversations and the chat list update live instead of polling.

- **The chat stream.** An open chat streams over server-sent events, fed through Redis pub/sub. It carries:
  - new messages;
  - run card updates as they happen;
  - a draft of the agent's reply, marked "writing…", which the final message replaces in place.
- **The person stream.** A per-person stream, covering every organisation the person is in, keeps the chat list's unread counts and entries live.
- **Reconnecting.** A reconnect catches up from the last event id, with no gaps and no duplicates.
- **Who can stream.** Anyone in the organisation can open any chat's stream, joined or not.
- **Closing.** Leaving the organisation closes the person's streams with the reason. Leaving a chat doesn't, since anyone can read it. Archiving turns an open chat read-only through its header event.
- **Limits.** Caps per process and per person keep load bounded.
- **Fallback.** If Redis is unavailable, the app falls back to periodic reads and resumes streaming by itself.
- **No more polling.** The one-second conversation poll and the five-second chat list poll are removed.
- **Test helper.** This ticket adds a test helper that reads server-sent events streams.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [01: Open chats, and the Jev check on posted messages](01-open-chats.md), [02: Chats without owners, and archiving chats](02-chats-without-owners.md), [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md)

**Status:** done

- [x] New messages arrive on the chat stream without the browser polling.
- [x] A reconnect with a last event id delivers every missed message exactly once, including a message published between subscribing and reading.
- [x] A run card moves from queued to briefing to running to finished on the stream.
- [x] A draft grows as the reply is written, and the final message replaces it in place. Opening a conversation mid-reply shows the draft so far.
- [x] When a turn fails, the draft is removed and the failure is shown. Drafts never reach the database.
- [x] Unread counts and chat list changes (appearing, joined, left, renamed, archived, unarchived) update live from the person stream.
- [x] People outside the organisation get a 404 on a chat's stream; members who haven't joined can open it.
- [x] Leaving the organisation sends "gone" with its reason, and the client stops reconnecting. Archiving a chat turns an open view read-only without closing it.
- [x] Over the per-process cap, the stream answers 503 with a retry hint. Over the per-person cap, the oldest stream is closed as replaced.
- [x] With Redis down, streams answer 503, the client falls back to periodic reads, and recovers once Redis is back.
- [x] Heartbeats run on the configured interval, and the proxy configurations have buffering off and timeouts above the heartbeat.
