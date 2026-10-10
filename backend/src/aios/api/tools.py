from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from aios.auth import CurrentUser
from aios.tools import TOOLS

router = APIRouter(prefix="/api")


class ToolOut(BaseModel):
    key: str
    name: str
    description: str
    # JSON schema of the tool's settings, and the settings a newly attached tool starts with.
    config_schema: dict[str, Any]
    default_config: dict[str, Any]


@router.get("/tools", response_model=list[ToolOut])
def list_tools(user: CurrentUser):
    """The tool library: every tool an agent can be given."""
    return [
        ToolOut(
            key=spec.key,
            name=spec.name,
            description=spec.description,
            config_schema=spec.config_model.model_json_schema(),
            default_config=spec.config_model().model_dump(mode="json"),
        )
        for spec in TOOLS.values()
    ]
