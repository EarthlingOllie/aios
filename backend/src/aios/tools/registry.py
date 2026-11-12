import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

# What a tool gives a run: LangChain tools the agent calls, or provider-run (server) tools, given
# to the model as their provider's tool definition (see `aios.runner.server_tools`).
AgentTool = BaseTool | dict[str, Any]


@dataclass(frozen=True)
class ToolOrganisation:
    """The organisation an agent version is saved, restored or run in, for a tool's
    organisation-aware check (`ToolSpec.check`): its id, and a session on its store."""

    organisation_id: int
    store: Session
    # The settings are new (being saved), not stored ones: set by `validate_tool`.
    new_settings: bool = False
    # A version being restored: like new settings, what they name of an external system (a
    # connector's tools) is checked against what it offers now. A run leaves out what's gone.
    restoring: bool = False
    # What builds tell the run about tools they left out (a connector that's unavailable, a tool
    # that vanished upstream): its model is told, and its turn notes them (`aios.turns.note`).
    notices: list[str] = field(default_factory=list, compare=False)


@dataclass(frozen=True)
class ToolSpec[C: BaseModel]:
    # Stored on agent versions, so never rename one. If a tool's behaviour changes in a way old
    # versions shouldn't pick up, register it under a new key instead.
    key: str
    name: str
    description: str
    # Per-agent settings. Every field needs a default: the defaults are the tool's settings when
    # it's first attached, and its JSON schema drives the settings form.
    config_model: type[C]
    # Builds what the run is given.
    build: Callable[[C], list[AgentTool]]
    # Settings that name something of an organisation's (a connector, say) are checked against
    # the organisation the version is saved, restored or run in: answers why they can't be used
    # there (they belong to another organisation, say), or None if they can. Tools whose
    # settings are the same everywhere have none.
    check: Callable[[C, ToolOrganisation], str | None] | None = None
    # Builds what the run is given from what the organisation it runs in holds (the workflows a
    # tool may start, say, for its description), in place of `build` whenever that organisation
    # is known. Tools that need nothing of the organisation's to be built have none.
    build_in: Callable[[C, ToolOrganisation], list[AgentTool]] | None = None


TOOLS: dict[str, ToolSpec] = {}


class ToolError(ValueError):
    """An unknown tool key, or settings its tool rejects."""


def register(spec: ToolSpec) -> ToolSpec:
    if spec.key in TOOLS:
        raise ValueError(f"Tool {spec.key!r} is already registered")
    TOOLS[spec.key] = spec
    return spec


def _parse(key: str, config: dict[str, Any], *, stored: bool = False) -> tuple[ToolSpec, BaseModel]:
    spec = TOOLS.get(key)
    if spec is None:
        raise ToolError(f"Unknown tool: {key}")
    if stored:
        # Settings saved under an older settings model: fields it has since dropped are ignored
        # (and fields it has added take their defaults), so stored settings keep working. Values
        # the current model rejects still fail.
        config = {k: v for k, v in config.items() if k in spec.config_model.model_fields}
    try:
        return spec, spec.config_model.model_validate(config)
    except ValidationError as exc:
        errors = "; ".join(_describe(e) for e in exc.errors())
        raise ToolError(f"Invalid settings for {spec.name}: {errors}") from exc


def _describe(error: dict[str, Any]) -> str:
    message = error["msg"].removeprefix("Value error, ")
    field = ".".join(map(str, error["loc"]))
    return f"{field}: {message}" if field else message


def _check(spec: ToolSpec, parsed: BaseModel, org: ToolOrganisation | None) -> None:
    if org is None or spec.check is None:
        return
    if reason := spec.check(parsed, org):
        raise ToolError(f"{spec.name} can't be used: {reason}")


def validate_tool(
    key: str, config: dict[str, Any], org: ToolOrganisation | None = None
) -> dict[str, Any]:
    """New settings, checked strictly, with defaults filled in: the canonical form that's stored.
    Given the organisation they're saved in, also checked against it (`ToolSpec.check`)."""
    spec, parsed = _parse(key, config)
    _check(spec, parsed, None if org is None else dataclasses.replace(org, new_settings=True))
    return parsed.model_dump(mode="json")


def check_stored_tool(
    key: str, config: dict[str, Any], org: ToolOrganisation | None = None
) -> None:
    """Raises `ToolError` if stored settings can no longer be built (see `build_tool`), or, given
    the organisation, can't be used in it (`ToolSpec.check`)."""
    spec, parsed = _parse(key, config, stored=True)
    _check(spec, parsed, org)


def unusable_tools(
    tools: Iterable[tuple[str, dict[str, Any]]], org: ToolOrganisation | None = None
) -> str | None:
    """Why stored tools, as (key, settings) pairs, can't all be built (`check_stored_tool`), one
    reason per tool that can't, or None if they can."""
    reasons = []
    for key, config in tools:
        try:
            check_stored_tool(key, config, org)
        except ToolError as exc:
            reasons.append(str(exc))
    return "; ".join(reasons) or None


def stored_settings(key: str, config: dict[str, Any]) -> BaseModel:
    """Stored settings parsed as `build_tool` parses them, for code that needs the same values."""
    return _parse(key, config, stored=True)[1]


def build_tool(
    key: str, config: dict[str, Any], org: ToolOrganisation | None = None
) -> list[AgentTool]:
    """Builds a tool from stored settings, which may predate its current settings model. Given
    the organisation it runs in, refuses (`ToolError`) settings that can't be used in it
    (`ToolSpec.check`)."""
    spec, parsed = _parse(key, config, stored=True)
    _check(spec, parsed, org)
    if org is not None and spec.build_in is not None:
        return spec.build_in(parsed, org)
    return spec.build(parsed)


NOTICES = "Tools left out of this turn:"


def with_notices(instructions: str, notices: list[str]) -> str:
    """An agent's instructions, telling its model about tools left out of the turn (builds'
    `ToolOrganisation.notices`), if any were."""
    if not notices:
        return instructions
    told = "\n".join([NOTICES, *(f"- {n}" for n in notices)])
    return f"{instructions}\n\n{told}" if instructions else told
