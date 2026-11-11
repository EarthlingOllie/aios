"""The organisation prefix: `/api/o/{organisation_id}/...`, where a request names the
organisation it means by its id (links in the browser use its slug).

Every route that resolves the request's organisation (its dependencies reach
`aios.auth.current_organisation`, which `CurrentOrganisation` and `CurrentDataStore` do) is
mounted under the prefix (`mount_organisation_prefix`), and only there: `/api/agents/{agent_id}`
is `/api/o/{organisation_id}/agents/{agent_id}`. Nothing is listed by hand, so a route added
later moves as soon as it takes the organisation. `current_organisation` reads the id from the
path and checks the signed-in user is a member. What isn't any one organisation's (signing in,
the person's profile and notifications, creating an organisation, invitations, the tool
library, timezones) stays unscoped.
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


def _copy(router: APIRouter, route, path: str) -> None:
    """Add `route` (an `APIRoute`'s context) to `router` at `path`, as it is: the same endpoint,
    dependencies and responses."""
    router.add_api_route(
        path,
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


def mount_organisation_prefix(app: FastAPI) -> None:
    """Move every organisation-scoped route of `app` under the prefix, as it is (the same
    endpoint, dependencies and responses); every other route stays where it is. Call once, after
    including every router."""
    routes = APIRouter()
    others = []
    for route in list(iter_route_contexts(app.router.routes)):
        if not isinstance(route.original_route, APIRoute):
            others.append(route.original_route)
        elif resolves_organisation(route.dependant):
            _copy(routes, route, prefixed(route.path))
        else:
            _copy(routes, route, route.path)
    app.router.routes[:] = others
    app.include_router(routes)
