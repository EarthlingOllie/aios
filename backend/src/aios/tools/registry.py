from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_ai.capabilities import AbstractCapability


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
    # Builds what the run is given. Function tools can be wrapped as
    # `pydantic_ai.capabilities.Toolset(FunctionToolset([...]))`.
    build: Callable[[C], AbstractCapability[Any]]


TOOLS: dict[str, ToolSpec] = {}


class ToolError(ValueError):
    """An unknown tool key, or settings its tool rejects."""


def register(spec: ToolSpec) -> ToolSpec:
    if spec.key in TOOLS:
        raise ValueError(f"Tool {spec.key!r} is already registered")
    TOOLS[spec.key] = spec
    return spec


def _parse(key: str, config: dict[str, Any]) -> tuple[ToolSpec, BaseModel]:
    spec = TOOLS.get(key)
    if spec is None:
        raise ToolError(f"Unknown tool: {key}")
    try:
        return spec, spec.config_model.model_validate(config)
    except ValidationError as exc:
        errors = "; ".join(_describe(e) for e in exc.errors())
        raise ToolError(f"Invalid settings for {spec.name}: {errors}") from exc


def _describe(error: dict[str, Any]) -> str:
    message = error["msg"].removeprefix("Value error, ")
    field = ".".join(map(str, error["loc"]))
    return f"{field}: {message}" if field else message


def validate_tool(key: str, config: dict[str, Any]) -> dict[str, Any]:
    """The tool's settings with defaults filled in, in the canonical form that's stored."""
    _, parsed = _parse(key, config)
    return parsed.model_dump(mode="json")


def build_tool(key: str, config: dict[str, Any]) -> AbstractCapability[Any]:
    spec, parsed = _parse(key, config)
    return spec.build(parsed)
