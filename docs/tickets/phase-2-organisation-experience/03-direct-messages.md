# 03: Direct messages, and copying one into a chat

**What to build:** Members can start a direct message with any people, agents and workflows in their organisation. It can have an optional name, and there's an option to reuse an existing one with exactly the same chat members.

- **Adding people.** Anyone in a direct message can add people, agents and workflows, who see its history from the start.
- **No removal.** Nobody can remove anyone from a direct message; leaving is the only way out.
- **Deletion.** When the last person leaves, the direct message is deleted with its messages, invocations and search entries. The same happens when the last person leaves the organisation.
- **Invisible from outside.** Direct messages are invisible to everyone outside them, admins included: they get a 404, as if it didn't exist.
- **Copying into a chat.** Any chat member of a direct message can copy it into a new named chat.
  - The new chat starts with a transcript of the direct message's text messages, and the copier is its only person.
  - The direct message is left unchanged, and nothing links back to it.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [02: Chats without owners, and archiving chats](02-chats-without-owners.md)

**Status:** ready-for-agent

- [ ] Creating, adding to, leaving and the last person leaving all work through the API and the "New direct message" screen.
- [ ] Creating with "reuse" returns the existing direct message with exactly those chat members.
- [ ] Every attempt to remove a person, agent or workflow from a direct message is refused.
- [ ] A person leaving the organisation as the last person in a direct message deletes it in the same transaction.
- [ ] An adversarial test has an admin and an outsider try every route that could reach a direct message: reads, members, the people who can be mentioned, run pages and run lists, search, and copying. Each answers 404 or leaves the direct message out.
- [ ] Copying produces a chat whose transcript holds text messages with their author and time, with no run cards or briefs. An outsider gets a 404, and a name clash gets a 409.
- [ ] The copy is recorded in the audit log, without content.
- [ ] Direct messages are listed apart from chats, and never show a description.
