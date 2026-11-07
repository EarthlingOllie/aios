# 11: Phase 2 full-stack scenarios; drop old chat and notification fields

**What to build:** Two things finish phase 2.

**Full-stack scenarios.** The phase 0 full-stack test gains three scenarios, proving phase 2 works end to end in a real browser:
- a direct message with an agent, whose reply streams in;
- a mentioned workflow whose result is posted as a handback, with the full output checked;
- an admin failing to open a direct message by its link.

**Removing the old forms.** Once at least one release has passed since they stopped being read:
- the chats' old visibility and the chat members' old role, with their types;
- the old notification reference keys and the redirecting notification routes.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [05: One chat experience: read-only run page, "Chat with this agent"](05-one-chat-experience.md), [08: Handback, and delivery to chats and direct messages](08-handback-and-delivery.md), [09: post_message tool](09-post-message.md)

**Status:** ready-for-agent

- [ ] The three full-stack scenarios pass in CI, sharded like the rest of the suite.
- [ ] No code reads the chats' old visibility, the chat members' old role, or the old notification reference keys.
- [ ] A store migration drops the old chat columns and their types, and runs cleanly across stores through the store migration command.
- [ ] The old notification routes are gone, and the browser tests use only the new ones.
- [ ] The docs pages for chats, triggers and tools, and the glossary, match the behaviour of phase 2.
