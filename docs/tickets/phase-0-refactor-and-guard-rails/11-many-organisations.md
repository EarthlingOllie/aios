# 11: Many organisations: signing up makes nothing, join, switcher, leave

**What to build:** people join organisations rather than being placed in one, and can be in any
number of them.

- **Signing up.** Signing up creates nothing. A person with no organisation lands on a welcome
  page, which creates an organisation (its store provisioned at once) or explains invitation
  links.
- **Joining.** The one-membership-per-person rule is dropped. Accepting an invitation adds a
  membership, idempotently. The empty-organisation rules, and the invite page's "not empty"
  path, are removed.
- **Switching.** The shell's organisation name becomes a switcher listing the person's
  organisations, with "Create an organisation".
- **Leaving.** Leaving removes only the membership, handing over the person's chats as today. It
  lands them on the organisation they used most recently, or on the welcome page.
  - The last member is told to delete the organisation instead.
  - The creator is refused while others remain. There's no transfer action; phase 1's roles close
    that gap.
- **Names.** Organisation names may repeat. Slugs stay unique.
- **Full-stack test.** It now starts from the welcome page.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:**
- [04: Full-stack test, with the scripted model as a selectable provider](04-full-stack-test.md)
- [10: Move every route and screen under the organisation (notifications get their organisation here)](10-move-routes-under-organisation.md)

**Status:** ready-for-agent

- [ ] A new registrant without an invitation has no organisations, nothing is provisioned, and
  they land on the welcome page.
- [ ] Creating an organisation from the welcome page provisions its store and opens it.
- [ ] Creating an organisation with another organisation's name is allowed.
- [ ] Registering through an invitation joins only that organisation.
- [ ] A person in one organisation accepts an invitation to another and is in both.
- [ ] Accepting the same invitation twice answers 200 the second time and adds nothing.
- [ ] The switcher lists every organisation the person is in and offers "Create an
  organisation".
- [ ] Leaving creates no organisation and lands on the most recently used remaining organisation,
  or on the welcome page if none remain.
- [ ] The last member is refused with "Delete the organisation instead", and the creator with
  others remaining is refused.
- [ ] The empty-organisation tests are removed, and the organisation, invitation, scoping and
  auth tests pass without personal organisations.
- [ ] The full-stack test passes, starting from the welcome page.
