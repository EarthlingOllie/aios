# 11: Phase 2 full-stack scenarios

**What to build:** The phase 0 full-stack test gains three scenarios, proving phase 2 works end to end in a real browser:
- a chat made with **Chat with this agent**, whose reply streams in;
- a mentioned workflow whose result is posted as a handback, with the full output checked;
- a member reading a chat they haven't joined, then posting and so joining it.

The docs pages are brought up to date at the same time.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [05: One chat experience: "Chat with this agent"](05-one-chat-experience.md), [08: Handback, and delivery to chats](08-handback-and-delivery.md), [09: post_message tool](09-post-message.md)

**Status:** ready-for-agent

- [ ] The three full-stack scenarios pass in CI, sharded like the rest of the suite.
- [ ] The docs pages for chats, triggers and tools, and the glossary, match the behaviour of phase 2.
