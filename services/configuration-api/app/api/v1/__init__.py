"""Versioned API surface (spec 67).

Routers are registered here as each resource lands. The base path is
``/api/v1`` and is configured, not hard-coded at call sites.
"""

from __future__ import annotations

from fastapi import APIRouter

api_router = APIRouter()

# Resource routers are mounted here as the phases deliver them:
#   tenants, pbxs, sip-trunks, phone-numbers, agents, providers, voices,
#   tools, knowledge-bases, routing-rules, calls, analytics, usage.
