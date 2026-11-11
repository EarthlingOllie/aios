"""The organisation prefix: `/api/o/{organisation_id}/...`, where a request names the
organisation it means by its id (links in the browser use its slug).

Every route that resolves the request's organisation (its dependencies reach
`aios.auth.current_organisation`, which `CurrentOrganisation` and `CurrentDataStore` do) is also
mounted under the prefix (`mount_organisation_prefix`), beside its unprefixed form:
`/api/agents/{agent_id}` is `/api/o/{organisation_id}/agents/{agent_id}` too. Nothing is listed
by hand, so a route added later is covered as soon as it takes the organisation. Under the
prefix, `current_organisation` reads the id from the path and checks the signed-in user is a
member; without it, it reads their one membership, as before, until the unprefixed forms go.
"""

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIRouter, iter_route_contexts

from aios.auth import current_organisation

API = "/api"
ORGANISATION_PREFIX = f"{API}/o/{{organisation_id}}"


def resolves_organisation(dependant: Dependant) -> bool:
    """Whether a route's dependencies resolve the request's organisation."""
    return dependant.call is current_organisation or any(
        resolves_organisation(d) for d in dependant.dependencies
    )


def prefixed(path: str) -> str:
    """An organisation-scoped API path under the prefix: `/api/x` is `/api/o/{id}/x`."""
    if not path.startswith(f"{API}/"):
        raise ValueError(f"{path} isn't an API path")
    return ORGANISATION_PREFIX + path.removeprefix(API)


def mount_organisation_prefix(app: FastAPI) -> None:
    """Mount every organisation-scoped route of `app` under the prefix too, as it is (the same
    endpoint, dependencies and responses). Call once, after including every router."""
    scoped = APIRouter()
    for route in list(iter_route_contexts(app.router.routes)):
        if not isinstance(route.original_route, APIRoute):
            continue
        if not resolves_organisation(route.dependant):
            continue
        scoped.add_api_route(
            prefixed(route.path),
            route.endpoint,
            response_model=route.response_model,
            status_code=route.status_code,
            tags=route.tags,
            dependencies=route.dependencies,
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=route.responses,
            deprecated=route.deprecated,
            methods=route.methods,
            response_model_include=route.response_model_include,
            response_model_exclude=route.response_model_exclude,
            response_model_by_alias=route.response_model_by_alias,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            response_model_exclude_none=route.response_model_exclude_none,
            include_in_schema=route.include_in_schema,
            response_class=route.response_class,
            name=route.name,
            callbacks=route.callbacks,
            openapi_extra=route.openapi_extra,
        )
    app.include_router(scoped)
