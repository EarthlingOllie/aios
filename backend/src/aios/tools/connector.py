"""The `connector` tool kind: an agent given connectors' actions, as their providers' tools,
served over MCP (`aios.mcp_client`). Registered under `connector`, never renamed.

**Settings:** the connectors (`connectors`), each pinned by its id and provider with the names of
the tools the agent may use, so a provider adding tools doesn't change what the agent does, and
the targets its calls may act on (`targets`: channels, repositories). Each connector is a group in
the agent editor's tool picker (`GET /connectors/{id}/tools`).

**Allowed targets.** A call naming a target (the channel or repository it acts on) that isn't on
the pin's `targets` is refused before it's made, and the model is told. A provider whose tools
need it (`Tools.targets_required`: GitHub) refuses every call without such a target, so an empty
list allows nothing; for the others an empty list allows any target.

**Limited tools.** A tool its provider limits to chosen targets (`Tools.limited`: Slack's
`post_to_slack`, to chosen channels) also has the targets it's allowed (`allowed`); a version
giving it none is refused.

**Checked** (`check`) whenever a version is saved, restored or run: a connector that isn't the
organisation's is refused, and one since removed too ("the Test connector was removed"), so a
version using it can't start. New settings, and a restored version's, are also checked against
what the connector offers now: a tool name it doesn't offer is refused.

**At the start of each turn** (`build_in`) the connector's tools are listed, cached per connector
for `connector_tool_cache_seconds`, and only the pinned names are given to the model, each named
`<provider>__<tool>`. A pinned tool that vanished upstream is left out, the turn still runs, and
its turn notes it (`ToolOrganisation.notices`). A connector that's paused, being removed, needs
reconnecting or whose provider is unavailable leaves all its tools out, and the model is told why.

**A call** goes to the connector's MCP server with its credential (`aios.connectors.credential`),
or, for a provider whose tools aios serves itself (GitHub), to the provider's own code with the
credential and the calling agent's name (what it posts is signed with it), checked again first,
so a connector stopped mid-turn stops its tools at once. The result is cut to
`connector_tool_result_cap_bytes`, saying so. It isn't on the trusted list, so it's checked by Jev
inline and wrapped as untrusted (`aios.tool_checks`): flagged, or not checked, it reaches the
agent as an error. A call to a tool with side effects (any not marked read-only) is recorded in
the audit log (`connector.tool_called`): the agent and run, the tool, its target (the channel or
repository it acts on), the turn and the person behind the invocation, never the content; and so
is a call refused for its target.

**A limited tool's call** on a target it isn't allowed is refused before anything is sent: the
model is told which targets it may use, and the refusal is audited (outcome `refused`). **A signed
tool's text** (`Tools.signed`) ends with "— <agent name> via aios", so what an agent posts says
who posted it (GitHub's tools, served by aios, sign their own).
"""

import asyncio
import concurrent.futures
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.concurrency import run_in_threadpool
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import audit, mcp_client, outbound, source_providers, usage
from aios.config import get_settings
from aios.connector_credentials import credential
from aios.datastores import store_id_of, store_session
from aios.datastores.models import Agent, Connector, ConnectorStatus, Source, SourceStatus
from aios.source_providers import NeedsReconnecting
from aios.tools.registry import AgentTool, ToolOrganisation, ToolSpec, register

log = logging.getLogger(__name__)

KEY = "connector"
SEPARATOR = "__"
EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

LEFT_OUT = "The {title} connector's tools aren't available in this turn: {why}"
VANISHED = "The {title} connector no longer offers the tool {name}, so it was left out."
CUT = "\n\n[Cut: the result was {size} bytes, over the {cap}-byte cap.]"
NOT_ALLOWED = (
    "{tool} may act only on the {arg}s it's allowed ({allowed}); {target} isn't one of them, so "
    "nothing was done."
)
NONE_ALLOWED = "{tool} isn't allowed any {arg}, so nothing was done."
SIGNATURE = "\n\n— {agent} via aios"


class ConnectorPin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: int
    provider: str = Field(min_length=1, max_length=64)
    # The connector's tools the agent may use, by name.
    tools: list[str] = Field(default_factory=list, max_length=200)
    # What its calls may act on (channels, repositories), by name.
    targets: list[str] = Field(default_factory=list, max_length=200)
    # A limited tool's allowed targets (channels, repositories), by tool name.
    allowed: dict[str, list[str]] = Field(
        default_factory=dict, max_length=200, exclude_if=lambda v: not v
    )

    @field_validator("tools", "targets")
    @classmethod
    def _distinct(cls, names: list[str]) -> list[str]:
        return list(dict.fromkeys(t.strip()[:255] for t in names if t.strip()))

    @field_validator("allowed")
    @classmethod
    def _targets(cls, allowed: dict[str, list[str]]) -> dict[str, list[str]]:
        cleaned = {}
        for tool, targets in allowed.items():
            if len(targets) > 1000:
                raise ValueError("at most 1000 targets per tool")
            cleaned[tool.strip()] = list(dict.fromkeys(t.strip() for t in targets if t.strip()))
        return {k: v for k, v in cleaned.items() if k}


class ConnectorToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connectors: list[ConnectorPin] = Field(
        default_factory=list,
        title="Connectors",
        description="Each connector's tools the agent may use.",
        json_schema_extra={"x-picker": "connectors"},
    )

    @field_validator("connectors")
    @classmethod
    def _one_each(cls, connectors: list[ConnectorPin]) -> list[ConnectorPin]:
        if len({c.connector_id for c in connectors}) != len(connectors):
            raise ValueError("each connector can be given once")
        return connectors


def title_of(provider: str) -> str:
    found = source_providers.provider(provider)
    return found.title if found else provider


# --- What a connector offers ---------------------------------------------------------------------


class Unavailable(Exception):
    """Why a connector's tools can't be used now, for the model."""


def unavailable(store: Session, connector: Connector) -> str | None:
    """Why the connector's tools can't be used now (None if they can)."""
    if connector.status == ConnectorStatus.needs_reauth:
        return "it needs reconnecting by an admin."
    if connector.status == ConnectorStatus.removing:
        return "it's being removed."
    paused = store.scalar(
        select(Source.id).where(
            Source.connector_id == connector.id, Source.status == SourceStatus.paused
        )
    )
    if paused is not None:
        return "an admin paused it."
    if why := source_providers.why_unavailable(connector.provider):
        return why
    found = source_providers.provider(connector.provider)
    if found is None or found.tools is None:
        return f"{title_of(connector.provider)} has no tools in this version of aios."
    return None


def _server(store_id: uuid.UUID, connector_id: int, provider: str) -> mcp_client.Server:
    """The connector's MCP server, called with its credential (`Unavailable` if it has none)."""
    found = source_providers.provider(provider)
    assert found is not None and found.tools is not None
    try:
        secret = credential(store_id, connector_id)
    except NeedsReconnecting as exc:
        raise Unavailable("it needs reconnecting by an admin.") from exc
    tools = found.tools

    def check(url) -> None:
        outbound.check(provider, url)

    return mcp_client.Server(
        tools.url,
        token=secret,
        transport=tools.transport() if tools.transport else None,
        check=check,
    )


# (store, connector) -> (when it was listed, by `time.monotonic`, and the listing).
_listings: dict[tuple[uuid.UUID, int], tuple[float, list[mcp_client.Tool]]] = {}


def forget_listings() -> None:
    _listings.clear()


async def offered(store_id: uuid.UUID, connector_id: int, provider: str) -> list[mcp_client.Tool]:
    """The connector's tools, as its server lists them, cached for
    `connector_tool_cache_seconds` (`Unavailable` if they can't be listed)."""
    found = source_providers.provider(provider)
    if found is not None and found.tools is not None and found.tools.listing is not None:
        return found.tools.listing()
    key = (store_id, connector_id)
    hit = _listings.get(key)
    if hit is not None and time.monotonic() - hit[0] < get_settings().connector_tool_cache_seconds:
        return hit[1]
    server = await run_in_threadpool(_server, store_id, connector_id, provider)
    try:
        tools = await mcp_client.list_tools(server)
    except mcp_client.McpError as exc:
        log.warning("couldn't list connector %s's tools", connector_id)
        raise Unavailable("its tools couldn't be listed just now.") from exc
    _listings[key] = (time.monotonic(), tools)
    return tools


def _blocking[T](make: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine to its end from synchronous code: here if no loop is running in this
    thread, else on a thread of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make())
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(make())).result()


def offered_now(store_id: uuid.UUID, connector: Connector) -> list[mcp_client.Tool]:
    """`offered`, from synchronous code."""
    connector_id, provider = connector.id, connector.provider
    return _blocking(lambda: offered(store_id, connector_id, provider))


# --- Checking settings ---------------------------------------------------------------------------


def check(config: ConnectorToolsConfig, org: ToolOrganisation) -> str | None:
    reasons = []
    for pin in config.connectors:
        title = title_of(pin.provider)
        connector = org.store.get(Connector, pin.connector_id)
        if connector is None or connector.provider != pin.provider:
            reasons.append(
                f"connector {pin.connector_id} isn't one of this organisation's"
                if org.new_settings
                else f"the {title} connector was removed"
            )
            continue
        if not (org.new_settings or org.restoring):
            continue
        try:
            if why := unavailable(org.store, connector):
                raise Unavailable(why)
            names = {t.name for t in offered_now(store_id_of(org.store), connector)}
        except Unavailable as exc:
            reasons.append(f"the {title} connector's tools can't be checked: {str(exc)[:-1]}")
            continue
        if missing := [n for n in pin.tools if n not in names]:
            reasons.append(f"the {title} connector doesn't offer {', '.join(missing)}")
        limited = _limited(pin.provider)
        for name in pin.tools:
            if name in limited and not pin.allowed.get(name):
                reasons.append(f"{name} needs at least one {limited[name]} it may use")
    return "; ".join(reasons) or None


# --- Calling -------------------------------------------------------------------------------------


def _capped(text: str) -> str:
    cap = get_settings().connector_tool_result_cap_bytes
    data = text.encode()
    if len(data) <= cap:
        return text
    return data[:cap].decode(errors="ignore") + CUT.format(size=len(data), cap=cap)


def _limited(provider: str) -> dict[str, str]:
    found = source_providers.provider(provider)
    return dict(found.tools.limited) if found and found.tools else {}


def _signed(provider: str) -> dict[str, str]:
    found = source_providers.provider(provider)
    return dict(found.tools.signed) if found and found.tools else {}


def _target(provider: str, arguments: dict[str, Any]) -> str | None:
    found = source_providers.provider(provider)
    names = found.tools.target_args if found and found.tools else ()
    for name in names:
        if isinstance(arguments.get(name), str | int):
            return str(arguments[name])
    return None


def _refusal(provider: str, tool: str, target: str | None, targets: list[str]) -> str | None:
    """Why a call acting on `target` isn't allowed by the pin's `targets` (None if it is)."""
    found = source_providers.provider(provider)
    tools = found.tools if found else None
    required = tools is not None and tools.targets_required
    if not required and (not targets or target is None):
        return None
    allowed = {t.casefold() for t in targets}
    if target is not None and target.casefold() in allowed:
        return None
    title, kind = title_of(provider), tools.targets_name if tools else "targets"
    if not targets:
        return (
            f"This agent isn't allowed to act on any {kind} with the {title} connector's {tool}, "
            "so nothing was done."
        )
    named = "no " + kind.rstrip("s") if target is None else target
    return (
        f"The {title} connector's {tool} may act only on this agent's allowed {kind} "
        f"({', '.join(targets)}); {named} isn't one of them, so nothing was done."
    )


def _ready(store_id: uuid.UUID, connector_id: int, agent_id: int | None) -> str:
    """Check the connector's tools can be used now (`Unavailable`); the calling agent's name."""
    with store_session(store_id, "ro") as store:
        connector = store.get(Connector, connector_id)
        if connector is None:
            raise Unavailable("it was removed.")
        if why := unavailable(store, connector):
            raise Unavailable(why)
        name = store.scalar(select(Agent.name).where(Agent.id == agent_id)) if agent_id else None
    return name or "an aios agent"


def _native(store_id: uuid.UUID, connector_id: int) -> str:
    """The connector's credential, for a tool aios serves itself (`Unavailable` if none)."""
    try:
        return credential(store_id, connector_id)
    except NeedsReconnecting as exc:
        raise Unavailable("it needs reconnecting by an admin.") from exc


def _audit(
    store_id: uuid.UUID,
    charge: usage.Charge | None,
    connector_id: int,
    provider: str,
    tool: str,
    target: str | None,
    outcome: str,
) -> None:
    actor = audit.SYSTEM if charge is None else audit.agent(charge.agent_id, charge.agent_run_id)
    title = title_of(provider)
    summary = f"Used {title}'s {tool}" + (f" on {target}" if target else "")
    if outcome == "refused":
        summary = f"Refused {title}'s {tool}" + (f" on {target}" if target else "")
    with store_session(store_id, "rw") as store:
        audit.record(
            store,
            actor,
            "connector.tool_called",
            "connector",
            connector_id,
            {
                "provider": provider,
                "tool": tool,
                "target": target,
                "turn_id": None if charge is None else charge.turn_id,
                "caused_by": None if charge is None else charge.person_id,
                "outcome": outcome,
                "summary": summary,
            },
        )
        store.commit()


async def call(
    store_id: uuid.UUID,
    connector_id: int,
    provider: str,
    tool: mcp_client.Tool,
    arguments: dict[str, Any],
    targets: list[str] | None = None,
    allowed: list[str] | None = None,
) -> str:
    """Call the connector's tool: its result (capped), or `ToolException` with why not. A limited
    tool may act only on its `allowed` targets."""
    title = title_of(provider)
    charge = usage.current()
    target = _target(provider, arguments)
    if refusal := _refusal(provider, tool.name, target, targets or []):
        if not tool.read_only:
            await run_in_threadpool(
                _audit, store_id, charge, connector_id, provider, tool.name, target, "refused"
            )
        raise ToolException(refusal)
    found = source_providers.provider(provider)
    native = found.tools if found and found.tools and found.tools.invoke else None
    try:
        agent = await run_in_threadpool(
            _ready, store_id, connector_id, None if charge is None else charge.agent_id
        )
        if native is not None:
            secret = await run_in_threadpool(_native, store_id, connector_id)
        else:
            server = await run_in_threadpool(_server, store_id, connector_id, provider)
    except Unavailable as exc:
        raise ToolException(LEFT_OUT.format(title=title, why=exc)) from exc
    arg = _limited(provider).get(tool.name)
    if arg is not None:
        limited_target = arguments.get(arg)
        if not isinstance(limited_target, str) or limited_target not in (allowed or []):
            await run_in_threadpool(
                _audit,
                store_id,
                charge,
                connector_id,
                provider,
                tool.name,
                str(limited_target) if limited_target is not None else None,
                "refused",
            )
            if not allowed:
                raise ToolException(NONE_ALLOWED.format(tool=tool.name, arg=arg))
            raise ToolException(
                NOT_ALLOWED.format(
                    tool=tool.name, arg=arg, allowed=", ".join(allowed), target=limited_target
                )
            )
    if text_arg := _signed(provider).get(tool.name):
        arguments = {
            **arguments,
            text_arg: str(arguments.get(text_arg) or "").rstrip() + SIGNATURE.format(agent=agent),
        }
    if native is not None:
        try:
            text, is_error = await run_in_threadpool(
                native.invoke, secret, tool.name, dict(arguments), agent
            )
        except Exception:
            log.exception("connector %s's tool %s failed", connector_id, tool.name)
            text, is_error = f"The {title} connector's tool {tool.name} failed just now.", True
        result = mcp_client.Result(text, is_error)
    else:
        try:
            result = await mcp_client.call_tool(server, tool.name, arguments)
        except mcp_client.McpError:
            result = mcp_client.Result(
                f"The {title} connector's tool {tool.name} couldn't be called just now.", True
            )
    if not tool.read_only:
        await run_in_threadpool(
            _audit,
            store_id,
            charge,
            connector_id,
            provider,
            tool.name,
            target,
            "failed" if result.is_error else "succeeded",
        )
    text = _capped(result.text)
    if result.is_error:
        raise ToolException(text)
    return text


def _agent_tool(
    store_id: uuid.UUID,
    connector_id: int,
    provider: str,
    tool: mcp_client.Tool,
    targets: list[str],
    allowed: list[str] | None = None,
) -> StructuredTool:
    async def run(**arguments: Any) -> str:
        return await call(store_id, connector_id, provider, tool, arguments, targets, allowed)

    description = f"[{title_of(provider)} connector] {tool.description}".strip()
    if arg := _limited(provider).get(tool.name):
        description += f" Allowed {arg}s: {', '.join(allowed or []) or 'none'}."
    return StructuredTool(
        name=f"{provider}{SEPARATOR}{tool.name}"[:64],
        description=description,
        args_schema=tool.input_schema or dict(EMPTY_SCHEMA),
        coroutine=run,
        handle_tool_error=True,
    )


def build_in(config: ConnectorToolsConfig, org: ToolOrganisation) -> list[AgentTool]:
    store_id = store_id_of(org.store)
    tools: list[AgentTool] = []
    for pin in config.connectors:
        title = title_of(pin.provider)
        # There: `check` ran first.
        connector = org.store.get_one(Connector, pin.connector_id)
        try:
            if why := unavailable(org.store, connector):
                raise Unavailable(why)
            listed = {t.name: t for t in offered_now(store_id, connector)}
        except Unavailable as exc:
            org.notices.append(LEFT_OUT.format(title=title, why=exc))
            continue
        for name in pin.tools:
            found = listed.get(name)
            if found is None:
                log.warning("connector %s no longer offers %s: left out", connector.id, name)
                org.notices.append(VANISHED.format(title=title, name=name))
                continue
            tools.append(
                _agent_tool(
                    store_id,
                    connector.id,
                    connector.provider,
                    found,
                    pin.targets,
                    pin.allowed.get(name),
                )
            )
    return tools


def build(config: ConnectorToolsConfig) -> list[AgentTool]:
    """Without the organisation (never at run time): nothing to call."""
    return []


register(
    ToolSpec(
        key=KEY,
        name="Connector tools",
        description="Act in connected systems (Slack, Google Drive, GitHub) with the connectors'"
        " tools picked for it.",
        config_model=ConnectorToolsConfig,
        build=build,
        check=check,
        build_in=build_in,
    )
)
