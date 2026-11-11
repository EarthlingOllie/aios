"""Mention guards: what posting a message checks, in this order, before anything costs money.

1. **Count.** The distinct agents and workflows a message mentions, at most
   `mentions_per_message` (5). Past it, a 422 and nothing is written. People don't count.
2. **Permission, per mention.** The target's **invocable by** (everyone, editors or admins)
   against the poster's role. A mention that fails it is still posted, its invocation refused
   with the reason on its card (`aios.invocations.create`); no model is called for it.
3. **Rate.** The allowed mentions are counted per person per organisation in Redis
   (`mention_rate_per_person`, 60 a minute). Past it, the whole post is a 429 with when to try
   again, and nothing is written. Refused mentions don't count.
4. **Budget.** Nothing yet (phase 3).
5. **The handoff**, on the run service, for each allowed mention.

Schedules and manual starts don't come through here: they skip the count and rate checks, and
are capped and charged like any turn (`aios.limits`, `aios.usage`).
"""

from collections.abc import Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import ratelimits
from aios.config import get_settings
from aios.datastores.models import Agent, Workflow
from aios.models import InvocableBy, Role, TriggerTargetKind

Target = tuple[TriggerTargetKind, int]

# Who each setting lets invoke it.
ALLOWED: dict[InvocableBy, frozenset[Role]] = {
    InvocableBy.everyone: frozenset(Role),
    InvocableBy.editors: frozenset({Role.admin, Role.editor}),
    InvocableBy.admins: frozenset({Role.admin}),
}
WHO = {InvocableBy.editors: "editors and admins", InvocableBy.admins: "admins"}


def check_count(targets: Sequence[Target]) -> None:
    """1. A 422 if the message mentions more agents and workflows than one message may."""
    most = get_settings().mentions_per_message
    if len(set(targets)) > most:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A message can mention at most {most} agents and workflows. Mention fewer, or"
            " split it into several messages.",
        )


def refusals(store: Session, role: Role, targets: Sequence[Target]) -> dict[Target, str]:
    """2. Why each mention the poster's `role` may not make is refused, by target."""
    refused: dict[Target, str] = {}
    for kind, model in ((TriggerTargetKind.agent, Agent), (TriggerTargetKind.workflow, Workflow)):
        ids = [i for k, i in targets if k == kind]
        if not ids:
            continue
        for i, name, invocable_by in store.execute(
            select(model.id, model.name, model.invocable_by).where(model.id.in_(ids))
        ):
            if role not in ALLOWED[invocable_by]:
                refused[kind, i] = f"Only {WHO[invocable_by]} can invoke the {kind.value} {name}."
    return refused


def check_rate(organisation_id: int, user_id: int, allowed: int) -> None:
    """3. Count the `allowed` mentions against the person's rate in the organisation: a 429
    with when to try again if they'd pass it (and then none are counted)."""
    if allowed:
        ratelimits.throttle(
            "mentions",
            get_settings().mention_rate_per_person,
            f"{organisation_id}:{user_id}",
            cost=allowed,
            detail="You've mentioned agents and workflows too often. Try again in {retry}"
            " seconds; your message hasn't been posted.",
        )


def check_budget() -> None:
    """4. A no-op until phase 3."""
