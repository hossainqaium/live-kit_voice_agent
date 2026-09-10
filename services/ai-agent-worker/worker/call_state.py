"""Call state tracking (spec 42, 43, 58).

Every transition is validated against the state machine and written to
``call_events``, so a finished call can be reconstructed rather than inferred
from its final state alone.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from shared.logging import get_logger
from shared.models import (
    CALL_STATE_TRANSITIONS,
    TERMINAL_CALL_STATES,
    CallState,
    HangupReason,
)

logger = get_logger(__name__)


class IllegalTransitionError(RuntimeError):
    """A transition the state machine does not allow.

    Raised rather than silently written: an impossible state sequence in the
    database is a bug that would otherwise surface much later as unexplainable
    analytics.
    """


class CallStateTracker:
    """Owns one call's state and its event history.

    Holds its own session factory rather than a session, because a call
    outlives any single transaction and a session held open for the duration
    would pin a pooled connection for the whole conversation.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        call_row_id: uuid.UUID,
        tenant_id: uuid.UUID,
        call_id: str,
        initial_state: CallState = CallState.ANSWERED,
    ) -> None:
        self._factory = session_factory
        self._call_row_id = call_row_id
        self._tenant_id = tenant_id
        self._call_id = call_id
        self._state = initial_state
        self._started = datetime.now(UTC)

    @property
    def state(self) -> CallState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_CALL_STATES

    async def transition(
        self,
        to_state: CallState,
        *,
        hangup_reason: HangupReason | None = None,
        detail: str | None = None,
        **payload: Any,
    ) -> None:
        """Move to ``to_state``, recording the transition."""
        from_state = self._state

        if from_state is to_state:
            return

        allowed = CALL_STATE_TRANSITIONS.get(from_state, frozenset())
        if to_state not in allowed:
            raise IllegalTransitionError(
                f"{from_state} -> {to_state} is not a legal transition; "
                f"allowed: {sorted(s.value for s in allowed)}"
            )

        now = datetime.now(UTC)
        terminal = to_state in TERMINAL_CALL_STATES

        async with self._factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE calls SET
                        state = :state,
                        hangup_reason = COALESCE(:hangup_reason, hangup_reason),
                        failure_detail = COALESCE(:detail, failure_detail),
                        end_time = CASE WHEN :terminal THEN :now ELSE end_time END,
                        duration_seconds = CASE
                            WHEN :terminal THEN
                                GREATEST(0, EXTRACT(EPOCH FROM (:now - start_time))::int)
                            ELSE duration_seconds
                        END,
                        updated_at = :now
                    WHERE id = :id
                    """
                ),
                {
                    "state": to_state.value,
                    "hangup_reason": hangup_reason.value if hangup_reason else None,
                    "detail": detail,
                    "terminal": terminal,
                    "now": now,
                    "id": self._call_row_id,
                },
            )
            await self._insert_event(
                session,
                event_type="state_changed",
                occurred_at=now,
                from_state=from_state,
                to_state=to_state,
                payload=payload,
            )
            await session.commit()

        self._state = to_state
        logger.info(
            "call_state_changed",
            extra={
                "from_state": from_state.value,
                "to_state": to_state.value,
                "hangup_reason": hangup_reason.value if hangup_reason else None,
                **payload,
            },
        )

    async def record_event(self, event_type: str, **payload: Any) -> None:
        """Record a non-transition event, e.g. a provider latency measurement."""
        now = datetime.now(UTC)
        async with self._factory() as session:
            await self._insert_event(
                session, event_type=event_type, occurred_at=now, payload=payload
            )
            await session.commit()

    async def _insert_event(
        self,
        session: Any,
        *,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        from_state: CallState | None = None,
        to_state: CallState | None = None,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO call_events (
                    id, tenant_id, call_id, event_type, from_state, to_state,
                    occurred_at, payload, created_at, updated_at
                ) VALUES (
                    :id, :tenant_id, :call_row_id, :event_type, :from_state, :to_state,
                    :occurred_at, CAST(:payload AS jsonb), :now, :now
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": self._tenant_id,
                "call_row_id": self._call_row_id,
                "event_type": event_type,
                "from_state": from_state.value if from_state else None,
                "to_state": to_state.value if to_state else None,
                "occurred_at": occurred_at,
                "payload": json.dumps(payload, default=str),
                "now": occurred_at,
            },
        )
