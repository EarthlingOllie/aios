# 12: Delete an organisation with a grace period; remove unscoped routes

**What to build:** an organisation's creator can delete it deliberately, with a grace period in
which the deletion can be cancelled. Admins can do this once phase 1 adds roles.

- **Scheduling.** The creator types the organisation's name. Every member is notified, and a
  banner shows the deletion date.
- **During the grace period** the organisation works normally, and its invitation links stop
  opening.
- **Cancelling** clears the schedule and notifies members again.
- **Deleting.** When the grace period ends, a background task:
  - records the deletion: name, slug, who scheduled it, when, and when it went;
  - detaches the store for the existing teardown to destroy, with its files and roles;
  - deletes the organisation.

  Members with no organisation left land on the welcome page.

This ticket also finishes the many-organisations work:
- any route without an organisation that's left, other than those explicitly unscoped, is
  removed;
- the global uniqueness of organisation names is dropped, so names may repeat while slugs stay
  unique;
- the personal flag is dropped;
- "personal" leaves the API and screens.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** [11: Many organisations: signing up makes nothing, join, switcher, leave](11-many-organisations.md)

**Status:** ready-for-agent

- [ ] Only the creator can schedule deletion, by typing the organisation's name. A wrong name
  answers 422, and scheduling twice answers 409.
- [ ] Every member gets an "organisation deletion scheduled" notification, and a banner shows the
  date.
- [ ] Cancelling within the grace period keeps everything and notifies members again.
- [ ] The grace period comes from a setting, 14 days by default, and the check interval from
  another, 10 minutes by default.
- [ ] Invitation links to an organisation scheduled for deletion stop opening.
- [ ] After the grace period:
  - [ ] the organisation is gone;
  - [ ] a deletion record exists;
  - [ ] the store reaches deleted through the existing teardown.
- [ ] Members whose only organisation was deleted land on the welcome page.
- [ ] No route without an organisation remains, other than those explicitly unscoped.
- [ ] Two organisations can share a name, and "personal" appears nowhere in the API or screens.
- [ ] The backend suite and full-stack test pass.
