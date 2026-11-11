"""The memory tools: `remember` and `forget` (`aios.memory`). Recall is the search tool's
(`aios.tools.search`), so an agent recalls its memories without either of these."""

import json
import logging
import uuid
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from aios import jev, memory
from aios.agent import ChatDeps
from aios.datastores import store_session
from aios.tools.data import in_store, in_thread
from aios.tools.registry import AgentTool, ToolSpec, register

log = logging.getLogger(__name__)

NO_RUN = "Memories can only be kept from an agent run: nothing was remembered."
FLAGGED = (
    "Not remembered: Jev, the prompt-injection check, flagged this text as a likely prompt"
    " injection. Nothing was stored."
)
UNCHECKED = (
    "Not remembered: Jev, the prompt-injection check, couldn't check this text just now, so"
    " nothing was stored. Try again in a moment."
)


class RememberConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _keep(data_store_id: uuid.UUID, run_id: int, content: str) -> dict:
    with store_session(data_store_id, "rw") as store:
        learned = memory.learned_in(store, run_id)
        if learned is None:
            return {"error": NO_RUN}
        try:
            kept, refreshed = memory.keep(store, learned, content)
        except memory.Refused as exc:
            store.rollback()
            return {"error": str(exc)}
        store.commit()
        return {"memory_id": kept.id, "refreshed": refreshed}


def _forget(data_store_id: uuid.UUID, run_id: int, memory_id: int) -> dict:
    with store_session(data_store_id, "rw") as store:
        learned = memory.learned_in(store, run_id)
        if learned is None:
            return {"error": "Memories can only be forgotten from an agent run."}
        if not memory.forget(store, learned.agent_id, memory_id, run_id):
            return {"error": f"You have no memory {memory_id}: nothing was forgotten."}
        store.commit()
        return {"forgotten": memory_id}


async def _remember_in(data_store_id: uuid.UUID, run_id: int, content: str) -> dict:
    return await in_thread(data_store_id, _keep, run_id, content)


async def _forget_in(data_store_id: uuid.UUID, run_id: int, memory_id: int) -> dict:
    return await in_thread(data_store_id, _forget, run_id, memory_id)


def _remember_description() -> str:
    return """Remember a fact you'll need in later runs, wherever you work: in any chat, or a run
with no chat. Keep each memory to one self-contained fact, written so it makes sense on its own.

Remembering the same text again (ignoring case and spacing) refreshes that memory instead of
adding another. Recall your memories with the `search` tool (kind `memory`): each result has
its `memory_id`, which `forget` takes. The text is checked for prompt injection first, and
refused if it's flagged or can't be checked; memories have a length limit, and you may keep only
so many: forget one you no longer need to make room."""


def build_remember(config: RememberConfig) -> list[AgentTool]:
    async def remember(
        content: Annotated[str, Field(min_length=1), "The fact to remember."],
        runtime: ToolRuntime[ChatDeps],
    ) -> str:
        ctx = runtime.context
        if ctx.run_id is None or ctx.data_store_id is None:
            return json.dumps({"error": NO_RUN})
        content = content.strip()
        if reason := memory.blank(content) or memory.too_long(content):
            return json.dumps({"error": reason})
        # Jev first: nothing it flags, or can't check, is stored.
        try:
            judgement = await jev.check(content, "memory")
        except jev.JevUnavailable as exc:
            log.warning("Didn't remember: %s", exc)
            return json.dumps({"error": UNCHECKED})
        if judgement.flagged:
            return json.dumps({"error": FLAGGED})
        return await in_store(runtime, _remember_in, ctx.run_id, content)

    return [StructuredTool.from_function(coroutine=remember, description=_remember_description())]


def build_forget(config: ForgetConfig) -> list[AgentTool]:
    async def forget(
        memory_id: Annotated[int, "The memory's id, as search returned it."],
        runtime: ToolRuntime[ChatDeps],
    ) -> str:
        ctx = runtime.context
        if ctx.run_id is None or ctx.data_store_id is None:
            return json.dumps({"error": "Memories can only be forgotten from an agent run."})
        return await in_store(runtime, _forget_in, ctx.run_id, memory_id)

    return [
        StructuredTool.from_function(
            coroutine=forget,
            description="Forget one of your memories that's no longer true or needed, by its"
            " memory_id (search with kind `memory` to find it).",
        )
    ]


register(
    ToolSpec(
        key="remember",
        name="Remember",
        description="Remember facts across runs, recalled through search wherever the agent"
        " works. Checked for prompt injection first.",
        config_model=RememberConfig,
        build=build_remember,
    )
)
register(
    ToolSpec(
        key="forget",
        name="Forget",
        description="Forget one of the agent's own memories.",
        config_model=ForgetConfig,
        build=build_forget,
    )
)
