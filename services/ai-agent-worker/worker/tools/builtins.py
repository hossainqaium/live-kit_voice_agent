"""Platform-provided tools the worker runs itself (spec 30).

HTTP tools call a tenant URL. These write to this platform or return sandbox
data so a development call can exercise function calling without a customer
API. ``create_ticket`` is the real one: it inserts a tenant ticket with
``source=AGENT``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.logging import get_logger
from shared.models import TicketPriority, TicketSource, TicketStatus
from shared.tools import builtin_name
from worker.tools.definitions import ToolDefinition, call_variables

logger = get_logger(__name__)

BuiltinHandler = Callable[
    [ToolDefinition, dict[str, Any], Any, async_sessionmaker[AsyncSession] | None],
    Awaitable[dict[str, Any]],
]

_CUSTOMERS = {
    "1001": {
        "customer_id": "1001",
        "name": "Ada Lovelace",
        "phone": "+15550001001",
        "tier": "gold",
    },
    "1002": {
        "customer_id": "1002",
        "name": "Grace Hopper",
        "phone": "+15550001002",
        "tier": "standard",
    },
}

_ORDERS = {
    "ORD-100": {
        "order_id": "ORD-100",
        "customer_id": "1001",
        "status": "shipped",
        "items": ["Widget"],
        "total": "48.00",
    },
    "ORD-200": {
        "order_id": "ORD-200",
        "customer_id": "1002",
        "status": "processing",
        "items": ["Gadget"],
        "total": "19.50",
    },
}

_INVENTORY = {
    "WIDGET": {"sku": "WIDGET", "name": "Widget", "available": 12},
    "GADGET": {"sku": "GADGET", "name": "Gadget", "available": 3},
}

_created_orders: dict[str, dict[str, Any]] = {}


def _priority(value: Any) -> TicketPriority:
    raw = str(value or TicketPriority.NORMAL.value).upper()
    try:
        return TicketPriority(raw)
    except ValueError:
        return TicketPriority.NORMAL


async def create_ticket(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    """File a support ticket attributed to this agent and call."""
    title = str(arguments.get("title") or "").strip()
    description = str(arguments.get("description") or "").strip()
    if not title or not description:
        return {"ok": False, "error": "title and description are required"}
    if factory is None:
        return {"ok": False, "error": "the ticket store is not available"}

    variables = call_variables(context)
    caller = arguments.get("caller_number") or variables.get("caller_number")
    now = datetime.now(UTC)
    ticket_id = uuid.uuid4()

    async with factory() as session:
        next_number = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT COALESCE(
                            MAX(CAST(SUBSTRING(ticket_number FROM 5) AS INTEGER)),
                            0
                        )
                        FROM tickets
                        WHERE tenant_id = :tenant_id
                          AND ticket_number ~ '^TCK-[0-9]+$'
                        """
                    ),
                    {"tenant_id": context.tenant_id},
                )
            ).scalar_one()
        )
        ticket_number = f"TCK-{next_number + 1:04d}"
        await session.execute(
            text(
                """
                INSERT INTO tickets (
                    id, tenant_id, ticket_number, title, description,
                    status, priority, source, caller_number, agent_id, call_id,
                    created_at, updated_at
                ) VALUES (
                    :id, :tenant_id, :ticket_number, :title, :description,
                    :status, :priority, :source, :caller_number, :agent_id, :call_id,
                    :now, :now
                )
                """
            ),
            {
                "id": ticket_id,
                "tenant_id": context.tenant_id,
                "ticket_number": ticket_number,
                "title": title[:255],
                "description": description,
                "status": TicketStatus.OPEN.value,
                "priority": _priority(arguments.get("priority")).value,
                "source": TicketSource.AGENT.value,
                "caller_number": str(caller)[:64] if caller else None,
                "agent_id": context.agent_id,
                "call_id": context.call_row_id,
                "now": now,
            },
        )
        await session.commit()

    logger.info(
        "ticket_filed_by_agent",
        extra={
            "ticket_number": ticket_number,
            "tool": definition.name,
            "agent_id": str(context.agent_id),
        },
    )
    return {
        "ok": True,
        "ticket_number": ticket_number,
        "status": TicketStatus.OPEN.value,
        "message": f"Ticket {ticket_number} has been filed.",
    }


async def get_customer(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    customer_id = str(arguments.get("customer_id") or "").strip()
    row = _CUSTOMERS.get(customer_id)
    if row is None:
        return {"ok": False, "error": f"no customer {customer_id}"}
    return {"ok": True, "customer": row}


async def check_order(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    order_id = str(arguments.get("order_id") or "").strip()
    row = _ORDERS.get(order_id) or _created_orders.get(order_id)
    if row is None:
        return {"ok": False, "error": f"no order {order_id}"}
    return {"ok": True, "order": row}


async def create_order(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    customer_id = str(arguments.get("customer_id") or "").strip()
    sku = str(arguments.get("sku") or "").strip().upper()
    if customer_id not in _CUSTOMERS:
        return {"ok": False, "error": f"no customer {customer_id}"}
    stock = _INVENTORY.get(sku)
    if stock is None or stock["available"] < 1:
        return {"ok": False, "error": f"{sku or 'that item'} is not available"}
    order_id = f"ORD-{100 + len(_created_orders) + len(_ORDERS)}"
    row = {
        "order_id": order_id,
        "customer_id": customer_id,
        "status": "accepted",
        "items": [stock["name"]],
        "total": "0.00",
    }
    _created_orders[order_id] = row
    return {"ok": True, "order": row}


async def cancel_order(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    order_id = str(arguments.get("order_id") or "").strip()
    row = _ORDERS.get(order_id) or _created_orders.get(order_id)
    if row is None:
        return {"ok": False, "error": f"no order {order_id}"}
    cancelled = {**row, "status": "cancelled"}
    _created_orders[order_id] = cancelled
    return {"ok": True, "order": cancelled}


async def check_inventory(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    sku = str(arguments.get("sku") or "").strip().upper()
    row = _INVENTORY.get(sku)
    if row is None:
        return {"ok": False, "error": f"no SKU {sku}"}
    return {"ok": True, "item": row}


async def send_sms(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    to = arguments.get("to") or call_variables(context).get("caller_number")
    body = str(arguments.get("body") or "").strip()
    if not to or not body:
        return {"ok": False, "error": "to and body are required"}
    return {"ok": True, "queued": True, "to": str(to), "sandbox": True}


async def send_email(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    to = str(arguments.get("to") or "").strip()
    subject = str(arguments.get("subject") or "").strip()
    if not to or not subject:
        return {"ok": False, "error": "to and subject are required"}
    return {"ok": True, "queued": True, "to": to, "sandbox": True}


async def transfer_call(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": (
            "warm transfer is not available yet. Tell the caller a human will "
            "call them back, and file a ticket if they reported a problem."
        ),
    }


async def refund_order(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    order_id = str(arguments.get("order_id") or "").strip()
    if order_id not in _ORDERS and order_id not in _created_orders:
        return {"ok": False, "error": f"no order {order_id}"}
    return {"ok": True, "order_id": order_id, "refund": "accepted", "sandbox": True}


HANDLERS: dict[str, BuiltinHandler] = {
    "create_ticket": create_ticket,
    "get_customer": get_customer,
    "check_order": check_order,
    "create_order": create_order,
    "cancel_order": cancel_order,
    "check_inventory": check_inventory,
    "send_sms": send_sms,
    "send_email": send_email,
    "transfer_call": transfer_call,
    "refund_order": refund_order,
}


async def execute_builtin(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    factory: async_sessionmaker[AsyncSession] | None,
) -> dict[str, Any]:
    name = builtin_name(definition.url_template) or definition.name
    handler = HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"no platform handler for {name}"}
    return await handler(definition, arguments, context, factory)
