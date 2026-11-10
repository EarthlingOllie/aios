# 08: Handback, and delivery to chats

**What to build:** Results that come back to a conversation are summarised before they're posted, and schedules can deliver their results to a chat.

**Handback.** It applies to a workflow's result after a mention, any delivered result, and an agent result longer than the threshold or in JSON.
- When the run ends, the full output is stored and the run card shows "Summarising".
- A queued step on the run service then posts a short summary, with **Full output** behind a disclosure on the card.
- If summarising fails, the raw output is posted, cut short, with a note. The invocation still succeeds.
- An agent's short reply to a mention is posted as written.
- Handbacks, delivered results and failure messages are derived text: stored with the verdict `derived`, never re-classified, and wrapped as untrusted wherever they enter model context. The asking message given to handback is wrapped too.

**Delivering to a chat.** A schedule can name any chat that isn't archived as its destination, whether or not the person setting it is in it. That adds its agent or workflow to the chat, and the trigger records who set the destination.
- When the run ends, it posts the summary with a collapsed run card, or a short failure message. Nothing is posted when the run starts.
- A failure notifies the trigger's creator and the admins.
- If the destination chat is archived, the run still runs but nothing is delivered. The trigger shows why, and whoever set the destination is notified.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [02: Chats without owners, and archiving chats](02-chats-without-owners.md), [07: Notifications across organisations: mentions, replies, failures](07-notifications.md), [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md), [Phase 1 · 13: Workflows node by node; approvals resume there; workflow worker removed](../phase-1-foundation/13-workflows-node-by-node.md)

**Status:** ready-for-agent

- [ ] Results are summarised or posted as written according to the rules: by kind, by length against the threshold, and by whether they're JSON.
- [ ] The full output is behind the card's disclosure, with JSON pretty-printed.
- [ ] A handback that times out or answers malformed posts the raw output, cut short, with a note, and the invocation is marked succeeded. A handback step whose lease was abandoned is retried, through the run service's drain entry point.
- [ ] Each handback call is traced (tagged handback) and recorded in the usage ledger.
- [ ] Posted handbacks and deliveries carry the verdict `derived`, and the scripted model receives them, and the asking message, inside untrusted blocks.
- [ ] **Deliver to** appears on the schedule forms and the Scheduler page. Choosing a chat adds the schedule's agent or workflow to it.
- [ ] A successful scheduled run posts one summary message, and a failed one posts one failure message and notifies the trigger's creator and the admins.
- [ ] After the destination chat is archived, the run still runs, nothing is delivered, the trigger shows the reason, and the setter is notified.
- [ ] Choosing an archived chat as a destination is refused with a clear reason.
