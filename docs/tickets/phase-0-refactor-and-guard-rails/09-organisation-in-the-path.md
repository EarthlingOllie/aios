# 09: Organisation in the path

**What to build:** the groundwork for people being in several organisations. Every organisation
gets a unique slug, backfilled from its name with numeric suffixes on clashes and never changed by
a rename. Links in the browser use the slug, and the API uses the id.

- **API.** The organisation can now be resolved from an organisation prefix in the request path,
  checking that the signed-in person is a member. The organisation prefix is mounted beside
  today's routes, which keep working and resolve to the person's most recently joined
  organisation.
- **"Me".** It lists the person's organisations: id, name, slug, and whether they're its creator.
- **Front end.** It understands organisation-prefixed links. An old link without one redirects to
  the same page in the organisation used last, remembered in the browser and falling back to the
  most recently joined.
- **Scoping test.** A test generated from the router checks that a person can't reach another
  organisation through any prefixed route.

This is the expand step of a wide refactor. Moving routes and screens follows in the next ticket.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** [02: Models split into a package by domain](02-models-package.md)

**Status:** ready-for-agent

- [ ] Every organisation has a unique slug, and two organisations named "Acme" get distinct
  slugs.
- [ ] Renaming an organisation leaves its slug unchanged.
- [ ] "Me" lists the person's organisations with id, name, slug and whether they're its creator.
- [ ] On every prefixed route, a non-member gets 404, the same as for an organisation that
  doesn't exist. The test is generated from the router, so new routes are covered automatically.
- [ ] The current-organisation dependency is still the only place a request's organisation is
  resolved.
- [ ] Old unprefixed routes still answer, for the most recently joined organisation.
- [ ] An old bookmark without an organisation redirects to the same page in the organisation
  used last.
- [ ] A link to an organisation the person isn't in shows "not found".
