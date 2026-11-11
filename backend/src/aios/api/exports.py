"""Exporting an agent or workflow version as a file, and importing one (`aios.exports`).

Both are an editor's: a participant gets a 403. An export is audited (`agent.exported`,
`workflow.exported`), and so is each thing an import makes (`skill.imported`, `agent.imported`,
`workflow.imported`, with the file's name); a dry run writes nothing.
"""

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from aios import audit, exports
from aios.api.agents.common import get_agent_version, get_org_agent
from aios.api.workflows import get_org_workflow, get_workflow_version
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.config import get_settings
from aios.db import DbId
from aios.permissions import Action, allow
from aios.tools import ToolOrganisation

router = APIRouter(prefix="/api")


def _attachment(file: dict, name: str, version: int) -> JSONResponse:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "export"
    return JSONResponse(
        file,
        headers={"Content-Disposition": f'attachment; filename="{slug}-v{version}.json"'},
    )


@router.get("/agents/{agent_id}/export", dependencies=[allow(Action.build)])
def export_agent(
    agent_id: DbId,
    user: CurrentUser,
    store: CurrentStore,
    version: Annotated[DbId | None, Query()] = None,
):
    """The agent's version `version` (its current one by default) as a file to import
    elsewhere."""
    agent = get_org_agent(store, agent_id)
    pinned = agent.current_version if version is None else get_agent_version(store, agent, version)
    file = exports.export_agent(pinned)
    audit.configured(store, user.id, "agent", agent, "exported", version=pinned.version)
    store.commit()
    return _attachment(file, agent.name, pinned.version)


@router.get("/workflows/{workflow_id}/export", dependencies=[allow(Action.build)])
def export_workflow(
    workflow_id: DbId,
    user: CurrentUser,
    store: CurrentStore,
    version: Annotated[DbId | None, Query()] = None,
):
    """The workflow's version `version` (its current one by default), with every agent, skill
    and workflow it runs, as a file to import elsewhere."""
    workflow = get_org_workflow(store, workflow_id)
    pinned = (
        workflow.current_version
        if version is None
        else get_workflow_version(store, workflow, version)
    )
    file = exports.export_workflow(pinned)
    audit.configured(store, user.id, "workflow", workflow, "exported", version=pinned.version)
    store.commit()
    return _attachment(file, workflow.name, pinned.version)


class ImportOut(BaseModel):
    dry_run: bool
    # The agent or workflow the file is of.
    root: exports.Planned
    # Everything made (or that would be): skills, then agents, then workflows.
    items: list[exports.Planned]


@router.post("/imports", response_model=ImportOut, dependencies=[allow(Action.build)])
def import_file(
    file: UploadFile,
    user: CurrentUser,
    org: CurrentOrganisation,
    store: CurrentStore,
    dry_run: bool = False,
):
    """Make new copies of what an exported file holds, at version 1; with `dry_run`, only say
    what would be made, renamed and left unable to run. A file over the size limit is a 413,
    one that isn't an export a 422; either way nothing is made."""
    limit = get_settings().export_max_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "That file is too large to import.")
    try:
        parsed = exports.parse(data)
    except exports.NotAnExport as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"That file isn't an export: {exc}"
        ) from exc
    the_plan = exports.plan(store, parsed, ToolOrganisation(org.id, store))
    if not dry_run:
        try:
            exports.apply(store, the_plan, user.id, file.filename or "a file")
            store.commit()
        except IntegrityError as exc:
            # A name taken by something saved since the plan read the names.
            store.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Something was saved with one of these names meanwhile. Try the import again.",
            ) from exc
    return ImportOut(dry_run=dry_run, root=the_plan.root, items=the_plan.items)
