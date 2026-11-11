"""Caps on what a turn and a workflow run may use: model calls, tokens and cost (in pounds
sterling, from the usage ledger, `aios.usage`).

| Cap | Name | Default setting |
| --- | --- | --- |
| Model calls per turn | `turn_model_calls` | `turn_max_model_calls` |
| Tokens per turn | `turn_tokens` | `turn_max_tokens` |
| Cost per turn | `turn_cost` | `turn_max_cost` |
| Model calls per workflow run | `run_model_calls` | `workflow_run_max_model_calls` |
| Cost per workflow run | `run_cost` | `workflow_run_max_cost` |

The limit in force is the agent's or workflow's own, else the organisation's (an admin's
override, `Organisation.limits`), else the deployment's (the settings). An agent sets the turn
caps, a workflow the run caps. Editors may lower an agent's or workflow's; only admins may set
one above the organisation's (`refuse_raise`, a 403 naming it).

A turn checks them before each model call (`aios.usage.UsageMiddleware`) and, at one, fails
with a message naming the limit (`reached`), keeping what it got to. A workflow agent node's
turn checks its workflow run's caps too.
"""

from collections.abc import Mapping
from typing import Annotated

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from aios.config import get_settings
from aios.models import Role

# By name: what a message calls it, and the setting its default is.
CAPS: dict[str, tuple[str, str]] = {
    "turn_model_calls": ("model calls per turn", "turn_max_model_calls"),
    "turn_tokens": ("tokens per turn", "turn_max_tokens"),
    "turn_cost": ("cost per turn", "turn_max_cost"),
    "run_model_calls": ("model calls per workflow run", "workflow_run_max_model_calls"),
    "run_cost": ("cost per workflow run", "workflow_run_max_cost"),
}
TURN = ("turn_model_calls", "turn_tokens", "turn_cost")
RUN = ("run_model_calls", "run_cost")

# What a turn stopped by a cap fails with starts with this.
REACHED = "Limit reached"
ONLY_ADMINS = (
    "Only admins can raise a limit above the organisation's ({label}: {limit}). Ask an admin."
)

Count = Annotated[int, Field(strict=True, ge=1, le=1_000_000_000)]
Cost = Annotated[float, Field(gt=0, le=1_000_000)]


class TurnLimits(BaseModel):
    """An agent's own turn caps; null follows the organisation's."""

    turn_model_calls: Count | None = None
    turn_tokens: Count | None = None
    turn_cost: Cost | None = None


class RunLimits(BaseModel):
    """A workflow's own run caps; null follows the organisation's."""

    run_model_calls: Count | None = None
    run_cost: Cost | None = None


class OrganisationLimits(TurnLimits, RunLimits):
    """An organisation's overrides of the deployment's caps; null keeps the default."""


Limits = dict[str, float]


def deployment() -> Limits:
    """The deployment's caps, by name."""
    settings = get_settings()
    return {name: getattr(settings, setting) for name, (_, setting) in CAPS.items()}


def overrides(values: Mapping | None) -> Limits:
    """The caps set in `values` (stored limits, or a request's), without the nulls."""
    return {k: v for k, v in (values or {}).items() if k in CAPS and v is not None}


def organisation(org_limits: Mapping | None) -> Limits:
    """The organisation's caps in force: its overrides, else the deployment's."""
    return deployment() | overrides(org_limits)


def in_force(org_limits: Mapping | None, *own: Mapping | None) -> Limits:
    """The caps in force for an agent or workflow (`own`, those of each, the later winning):
    its own, else the organisation's, else the deployment's."""
    limits = organisation(org_limits)
    for values in own:
        limits |= overrides(values)
    return limits


def label(name: str) -> str:
    return CAPS[name][0]


def amount(name: str, value: float) -> str:
    """A cap's value as a message says it: pounds for a cost."""
    return f"£{value:,.2f}" if name.endswith("_cost") else f"{value:,.0f}"


def reached(name: str, limit: float) -> str:
    """Why a turn stopped at the cap `name`: "Limit reached: at most 3 model calls per turn"."""
    of = " of" if name.endswith("_cost") else ""
    return f"{REACHED}: at most {amount(name, limit)}{of} {label(name)}."


def refuse_raise(
    role: Role, org_limits: Mapping | None, old: Mapping | None, new: Mapping | None
) -> None:
    """A 403 if a non-admin sets an agent's or workflow's cap (`new`, from `old`) above the
    organisation's. One an admin already set above it may be kept as it is."""
    if role == Role.admin:
        return
    ceiling = organisation(org_limits)
    before = overrides(old)
    for name, value in overrides(new).items():
        if value > ceiling[name] and before.get(name) != value:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                ONLY_ADMINS.format(label=label(name), limit=amount(name, ceiling[name])),
            )
