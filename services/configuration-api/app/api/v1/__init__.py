"""Versioned API surface (spec 67).

The base path is ``/api/v1`` and is configured, not hard-coded at call sites.

``reject_client_tenant_id`` is applied to the whole router rather than to
individual endpoints: spec 7 forbids trusting a browser-supplied tenant ID, and
a per-endpoint guard would be one forgotten decorator away from not holding.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1 import auth
from app.core.dependencies import reject_client_tenant_id

api_router = APIRouter(dependencies=[Depends(reject_client_tenant_id)])

api_router.include_router(auth.router)

# Resource routers are mounted here as the phases deliver them:
#   tenants, pbxs, sip-trunks, phone-numbers, agents, providers, voices,
#   tools, knowledge-bases, routing-rules, calls, analytics, usage.
