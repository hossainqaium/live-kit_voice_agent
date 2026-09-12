"""In-process handle so ``transfer_call`` can start a live warm transfer."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from worker.transfer.engine import WarmTransfer

_ACTIVE: dict[str, WarmTransfer] = {}


def register(transfer: WarmTransfer) -> None:
    _ACTIVE[transfer.call_id] = transfer


def unregister(call_id: str) -> None:
    _ACTIVE.pop(call_id, None)


def get(call_id: str) -> WarmTransfer | None:
    return _ACTIVE.get(call_id)
