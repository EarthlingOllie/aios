"""The organisation prefix: `/api/o/{organisation_id}/...`, where a request names the
organisation it means by its id (links in the browser use its slug).

Every route that resolves the request's organisation (its dependencies reach
`aios.auth.current_organisation`, which `CurrentOrganisation` and `CurrentDataStore` do) is
mounted under the prefix (`mount_organisation_prefix`), and only there: `/api/agents/{agent_id}`
is `/api/o/{organisation_id}/agents/{agent_id}`. Nothing is listed by hand, so a route added
later moves as soon as it takes the organisation. `current_organisation` reads the id from the
path and checks the signed-in user is a member. Each of them declares one permission check
(`aios.permissions.allow`), which says what its action is and which roles may take it: one that
doesn't stops the app starting (`UnpermittedRoute`).

What isn't any one organisation's (signing in, the person's profile and notifications, creating
an organisation, invitations, the tool library, timezones) stays unscoped, and only what's
listed in `UNSCOPED`: an API route that neither takes the organisation nor is listed there
stops the app starting (`UnscopedRoute`), so none is left without an organisation by mistake.
"""

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIRouter, iter_route_contexts

from aios.auth import current_organisation
from aios.permissions import Permit, permit_of

API = "/api"
ORGANISATION_PREFIX = f"{API}/o/{{organisation_id}}"

# The API routes explicitly without an organisation, as (method, path): each is the person's, or
# no one's, rather than any organisation's.
UNSCOPED: set[tuple[str, str]] = {
    # Signing in, and the development picker (`aios.dev_auth`) when it's on.
    ("GET", "/api/auth/options"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/dev/users"),
    ("POST", "/api/dev/session"),
    # The person's profile, with every organisation they're in.
    ("GET", "/api/me"),
    ("PATCH", "/api/me"),
    ("PUT", "/api/me/email"),
    ("PUT", "/api/me/password"),
    # The person's live updates, across every organisation they're in (`aios.api.streams`).
    ("GET", "/api/me/stream"),
    # An invitation link's page, and accepting it.
    ("GET", "/api/invites/{token}"),
    ("POST", "/api/invites/{token}/accept"),
    # Creating an organisation (the welcome page's).
    ("POST", "/api/organisations"),
    # The person's notifications, from every organisation they're in.
    ("GET", "/api/me/notifications"),
    ("POST", "/api/me/notifications/read"),
    ("POST", "/api/me/notifications/{notification_id}/read"),
    # The tool library and the timezones schedules can use: the same for everyone.
    ("GET", "/api/tools"),
    ("GET", "/api/triggers/timezones"),
    # Webhook triggers' public endpoint: no one signs in; the token leads to the organisation
    # (`aios.api.webhooks`).
    ("POST", "/api/webhooks/{token}"),
}


class UnscopedRoute(RuntimeError):
    """An API route that neither takes the organisation nor is listed in `UNSCOPED`."""


class UnpermittedRoute(RuntimeError):
    """A route of an organisation that doesn't declare exactly one permission check
    (`aios.permissions.allow`)."""


def permits(dependant: Dependant) -> list[Permit]:
    """The permission checks among a route's dependencies, however deep."""
    found = [p] if (p := permit_of(dependant.call)) is not None else []
    for d in dependant.dependencies:
        found += permits(d)
    return found


def route_permit(route) -> Permit:
    """The one permission check a route of an organisation declares (else `UnpermittedRoute`)."""
    found = permits(route.dependant)
    if len(found) != 1:
        raise UnpermittedRoute(
            f"{sorted(route.methods)} {route.path} declares {len(found)} permission checks, "
            "not one: give it `dependencies=[allow(Action...)]` (aios.permissions)"
        )
    return found[0]


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


def _check_unscoped(route) -> None:
    """Refuse an API route without an organisation unless it's listed in `UNSCOPED`."""
    if not route.path.startswith(f"{API}/"):
        return
    for method in sorted(route.methods):
        if (method, route.path) not in UNSCOPED:
            raise UnscopedRoute(
                f"{method} {route.path} doesn't take the organisation, and isn't listed in "
                "aios.api.scoping.UNSCOPED"
            )


def mount_organisation_prefix(app: FastAPI) -> None:
    """Move every organisation-scoped route of `app` under the prefix, as it is (the same
    endpoint, dependencies and responses); every other route stays where it is, if it's outside
    the API or listed in `UNSCOPED` (else `UnscopedRoute`). Call once, after including every
    router."""
    routes = APIRouter()
    others = []
    for route in list(iter_route_contexts(app.router.routes)):
        if not isinstance(route.original_route, APIRoute):
            others.append(route.original_route)
        elif resolves_organisation(route.dependant):
            route_permit(route)
            _copy(routes, route, prefixed(route.path))
        else:
            _check_unscoped(route)
            _copy(routes, route, route.path)
    app.router.routes[:] = others
    app.include_router(routes)
