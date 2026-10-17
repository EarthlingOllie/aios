"""The agent tool that asks for a change to the run's data store's tables:
`request_schema_change`.

The model sends a spec, never SQL (`aios.datastores.schema_changes`). The tool checks it against
the store's current schema, records a pending request, and pauses the run (LangGraph's
`interrupt()`): the turn ends with the request waiting for the owner. Once it's decided (and, if
approved, applied), the run is resumed with the outcome (`aios.api.agents.resume_run`), which is
what the tool call returns to the model.

Resuming runs the tool call again from the start, so it looks for the request its call already
made before making one, and `interrupt()` then returns the outcome.
"""

import json
import uuid
from typing import Annotated, Any

from anyio import to_thread
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aios.agent import ChatDeps
from aios.datastores import schema_changes
from aios.datastores.schema_changes import EMBEDDING_DIM, Invalid, Operation, SchemaChange
from aios.tools.data import in_store, in_thread
from aios.tools.registry import AgentTool, ToolSpec, register

# What the interrupt's value names the request by.
REQUEST_KEY = "schema_change_request"

IN_WORKFLOW = (
    "Schema changes can't be requested from a workflow run yet, only from the agent's own chat"
    " or schedules: nothing was requested."
)


class RequestSchemaChangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _propose(
    data_store_id: uuid.UUID, change: SchemaChange, reason: str, run_id: int, call_id: str
) -> dict:
    try:
        request = schema_changes.propose(
            data_store_id, change, reason, run_id=run_id, tool_call_id=call_id
        )
    except Invalid as exc:
        return {"error": f"{exc}. Nothing was requested."}
    return {"request_id": str(request.id)}


async def _request(
    data_store_id: uuid.UUID, change: SchemaChange, reason: str, run_id: int, call_id: str
) -> dict:
    return await in_thread(data_store_id, _propose, change, reason, run_id, call_id)


def _description() -> str:
    return f"""Ask the user to change the tables in their data store: create a table, add a
column, or create an index. Tables in the `data` schema are changed only this way, once the
user approves.

- `ops`: the changes, applied in order, all together or not at all:
  - `create_table`: `name`, `description`, `columns`, and `primary_key` (column names; left
    out, the table gets a `bigint` identity column `id`).
  - `add_column`: `table` and `column`.
  - `create_index`: `table`, `columns`, `method` (`btree` by default; `gin` for a jsonb column;
    `fulltext` for searching a text column's words; `hnsw` for nearest neighbours of a vector
    or embedding column), `unique` (btree only), and an optional `name`.
- A column has a `name`, a `type` (text, integer, bigint, numeric, boolean, date, timestamptz,
  jsonb, uuid, vector or embedding), `nullable` (default true), an optional `default` (a
  constant; "now" for a date or timestamptz; "random" for a uuid) and a `description`. A
  `vector` needs `dimensions`. An `embedding` is a vector({EMBEDDING_DIM}) of a text column in
  the same table, named by `embedding_of`, which `write_data` fills in for you.
- Names are lowercase letters, digits and underscores, starting with a letter, and not SQL
  reserved words.
- `reason`: why, for the user: they see it next to the exact SQL.

Put everything one task needs in one request. Call `describe_data` first to see the tables.
The user decides when they can: this turn ends, and the result comes once they approve (and
the change is applied) or reject it (maybe with a note). It says if it was applied, rejected,
failed, or no longer applies because the schema changed meanwhile."""


def build(config: RequestSchemaChangeConfig) -> list[AgentTool]:
    async def request_schema_change(
        ops: Annotated[list[Operation], "The changes, in order."],
        reason: Annotated[str, Field(min_length=1, max_length=2000), "Why, for the user."],
        runtime: ToolRuntime[ChatDeps],
    ) -> str:
        ctx, call_id = runtime.context, runtime.tool_call_id
        if ctx.workflow_run_id is not None or ctx.run_id is None:
            return json.dumps({"error": IN_WORKFLOW})
        request = await to_thread.run_sync(schema_changes.find, ctx.run_id, call_id)
        if request is None:
            try:
                change = SchemaChange.model_validate(
                    {"ops": [o.model_dump() if isinstance(o, BaseModel) else o for o in ops]}
                )
            except ValidationError as exc:
                return json.dumps({"error": f"The request isn't valid: {exc}"})
            made: dict[str, Any] = json.loads(
                await in_store(runtime, _request, change, reason, ctx.run_id, call_id)
            )
            if "error" in made:
                return json.dumps(made)
            request_id = made["request_id"]
        else:
            request_id = str(request.id)
        # Pauses the run here; when it's resumed, the outcome (`schema_changes.outcome`).
        outcome = interrupt({REQUEST_KEY: request_id})
        return json.dumps(outcome)

    return [
        StructuredTool.from_function(coroutine=request_schema_change, description=_description())
    ]


register(
    ToolSpec(
        key="request_schema_change",
        name="Request schema changes",
        description="Ask you to approve new tables, columns and indexes in your data store.",
        config_model=RequestSchemaChangeConfig,
        build=build,
    )
)
