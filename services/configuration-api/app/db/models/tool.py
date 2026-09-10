"""HTTP API tools and their permissions.

Spec 30 (function calling), 31 (API tool builder), 32 (tool permissions).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    TenantOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_column,
    json_column,
)
from shared.models import HttpMethod, ResourceStatus, ToolAuthType


class Tool(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A tenant-configured HTTP tool the agent can call (spec 30, 31)."""

    __tablename__ = "tools"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tools_tenant_name"),
        # Timeouts are bounded because a tool call happens inside a live
        # conversation. Anything above a few seconds means dead air, so the
        # ceiling is a product constraint rather than a safety net.
        CheckConstraint("timeout_seconds > 0 AND timeout_seconds <= 30", name="timeout_in_range"),
        CheckConstraint("max_retries >= 0 AND max_retries <= 5", name="max_retries_in_range"),
    )

    #: The function name exposed to the LLM, e.g. ``check_order``. Must be a
    #: valid identifier: providers reject names with spaces or punctuation.
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Shown to the model, so this text decides whether the tool gets called
    #: at the right moment. It is prompt surface, not documentation.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    http_method: Mapped[HttpMethod] = enum_column(
        HttpMethod, nullable=False, default=HttpMethod.GET
    )

    #: Supports ``{{variable}}`` substitution, e.g.
    #: https://api.example.com/orders/{{order_id}} (spec 31).
    url_template: Mapped[str] = mapped_column(String(2048), nullable=False)

    #: Static headers merged with auth. Values also support substitution.
    headers: Mapped[dict] = json_column(nullable=False, default=dict)

    #: JSON Schema for the arguments. Validated before publish (spec 31), and
    #: again on every call so a model cannot invent arguments.
    request_schema: Mapped[dict] = json_column(nullable=False, default=dict)

    #: Optional JSON Schema for the response, used to trim payloads before
    #: they reach the prompt.
    response_schema: Mapped[dict] = json_column(nullable=False, default=dict)

    auth_type: Mapped[ToolAuthType] = enum_column(
        ToolAuthType, nullable=False, default=ToolAuthType.NONE
    )

    #: Header name for API_KEY_HEADER auth, e.g. "X-API-Key".
    auth_header_name: Mapped[str | None] = mapped_column(String(128))

    #: Encrypted credential (spec 54). Null when auth_type is NONE.
    auth_secret_ciphertext: Mapped[bytes | None] = mapped_column(postgresql.BYTEA)
    encryption_key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    #: Whether the schema last validated cleanly. Publishing an agent that
    #: uses an invalid tool is blocked (spec 31, 63).
    schema_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schema_validation_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Tool {self.name}>"


class ToolPermission(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A constraint on how a tool may be used (spec 32).

    Separate from ``agent_tools``, which says *whether* an agent may call a
    tool. This says *under what limits* — so a tool can be shared by several
    agents while one of them is rate-limited or restricted to reads.
    """

    __tablename__ = "tool_permissions"
    __table_args__ = (
        UniqueConstraint("tool_id", "agent_id", name="uq_tool_permissions_tool_agent"),
        CheckConstraint(
            "max_calls_per_conversation IS NULL OR max_calls_per_conversation > 0",
            name="max_calls_per_conversation_positive",
        ),
    )

    tool_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("tools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Null means the limit applies to every agent using this tool.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )

    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Stops a looping model from calling the same endpoint indefinitely
    #: within one conversation.
    max_calls_per_conversation: Mapped[int | None] = mapped_column(Integer, default=5)

    #: Argument values the agent may not supply, for narrowing a broad
    #: endpoint without changing the tenant's API.
    denied_arguments: Mapped[dict] = json_column(nullable=False, default=dict)
