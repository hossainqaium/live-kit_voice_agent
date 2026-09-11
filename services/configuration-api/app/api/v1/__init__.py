"""Versioned API surface (spec 67).

The base path is ``/api/v1`` and is configured, not hard-coded at call sites.

``reject_client_tenant_id`` is applied to the whole router rather than to
individual endpoints: spec 7 forbids trusting a browser-supplied tenant ID, and
a per-endpoint guard would be one forgotten decorator away from not holding.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1 import (
    admin,
    agents,
    auth,
    calls,
    pbxs,
    phone_numbers,
    platform,
    routing,
    sip_trunks,
    tools,
)
from app.core.dependencies import reject_client_tenant_id

api_router = APIRouter(dependencies=[Depends(reject_client_tenant_id)])

api_router.include_router(auth.router)
api_router.include_router(pbxs.router)
api_router.include_router(sip_trunks.router)
api_router.include_router(phone_numbers.router)
api_router.include_router(agents.router)
api_router.include_router(calls.router)
api_router.include_router(routing.router)
api_router.include_router(tools.router)
api_router.include_router(admin.router)

# Platform console last, and under its own /platform prefix, so a tenant route
# can never be shadowed by a platform one added later.
api_router.include_router(platform.router)
