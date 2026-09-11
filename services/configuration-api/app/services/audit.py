"""Audit logging (spec 69).

Every configuration change records who, when, what, and the before and after
values. An authorization model with no audit trail cannot be reviewed after an
incident, which is the moment it matters most.

The trail is append-only: nothing in the application updates or deletes a row.
An audit log an operator can edit is not evidence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.db.models import AuditLog
from shared.logging import get_logger

logger = get_logger(__name__)

#: Field names whose values must never reach an audit row. An audit trail that
#: records a credential becomes a way to read one (spec 54), which would make
#: `billing.read` or `agents.read` a path to another tenant's API keys.
_SENSITIVE_HINTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "ciphertext",
    "authorization",
    "private_key",
)

_REDACTED = "***redacted***"


def redact(value: Any) -> Any:
    """Recursively replace secret-bearing values.

    Recursive, unlike the log redactor: an audit payload is a nested
    representation of a resource, so a credential can sit several levels down —
    inside a provider's config object, or in a list of trunk definitions.

    Only strings and bytes are replaced. A count or a timestamp whose name
    happens to contain "token" is real information, and destroying it was a bug
    worth not repeating.
    """
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if isinstance(inner, str | bytes)
                and any(hint in str(key).lower() for hint in _SENSITIVE_HINTS)
                else redact(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        # Ciphertext and similar: record that something was there, not what.
        return f"<{len(value)} bytes>"
    return value


async def record(
    session: AsyncSession,
    *,
    principal: Principal | None,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    tenant_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    request_id: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Append one audit row.

    Staged on the caller's session rather than committed here, so the audit
    entry and the change it describes land in the same transaction. A committed
    change with no audit row — or an audit row for a change that rolled back —
    would both be misleading.

    ``action`` is a dotted name matching spec 69: ``agent.published``,
    ``credential.rotated``, ``trunk.updated``.
    """
    # The principal's tenant wins when present: it comes from a verified token,
    # while an explicit argument is caller-supplied.
    effective_tenant = principal.tenant_id if principal is not None else tenant_id
    if effective_tenant is None:
        effective_tenant = tenant_id

    session.add(
        AuditLog(
            tenant_id=effective_tenant,
            user_id=principal.user_id if principal else None,
            user_email=principal.email if principal else None,
            occurred_at=datetime.now(UTC),
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            old_value=redact(old_value) if old_value is not None else None,
            new_value=redact(new_value) if new_value is not None else None,
            ip_address=ip_address,
            user_agent=user_agent[:1024] if user_agent else None,
            request_id=request_id,
        )
    )

    logger.info(
        "audit",
        extra={
            "audit_action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id) if resource_id is not None else None,
        },
    )


def snapshot(instance: Any, *fields: str) -> dict[str, Any]:
    """Capture named fields from a model instance for an audit value.

    Explicit field lists rather than the whole row: a full dump would grow
    silently as columns are added, and would pull ciphertext columns into the
    trail by default.
    """
    captured: dict[str, Any] = {field: getattr(instance, field, None) for field in fields}
    redacted: dict[str, Any] = redact(captured)
    return redacted
