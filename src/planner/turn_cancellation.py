from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CancelledTurn:
    request_id: str
    reason: str
    cancelled_at: float


class TurnCancellationStore:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._cancelled: dict[tuple[str, str], CancelledTurn] = {}

    def begin_turn(self, *, user_id: str, request_id: str, reason: str) -> str | None:
        with self._lock:
            self._prune_locked()
            previous = self._active.get(user_id)
            self._active[user_id] = request_id
            if previous and previous != request_id:
                self._cancelled[(user_id, previous)] = CancelledTurn(
                    request_id=previous,
                    reason=reason,
                    cancelled_at=time.monotonic(),
                )
                return previous
            return None

    def cancel_turn(
        self,
        *,
        user_id: str,
        request_id: str | None = None,
        reason: str,
    ) -> str | None:
        with self._lock:
            self._prune_locked()
            target = request_id or self._active.get(user_id)
            if not target:
                return None
            self._cancelled[(user_id, target)] = CancelledTurn(
                request_id=target,
                reason=reason,
                cancelled_at=time.monotonic(),
            )
            if self._active.get(user_id) == target:
                self._active.pop(user_id, None)
            return target

    def cancellation(self, *, user_id: str, request_id: str) -> CancelledTurn | None:
        with self._lock:
            self._prune_locked()
            return self._cancelled.get((user_id, request_id))

    def finish_turn(self, *, user_id: str, request_id: str) -> None:
        with self._lock:
            if self._active.get(user_id) == request_id:
                self._active.pop(user_id, None)

    def _prune_locked(self) -> None:
        deadline = time.monotonic() - self.ttl_seconds
        stale = [
            key
            for key, value in self._cancelled.items()
            if value.cancelled_at < deadline
        ]
        for key in stale:
            self._cancelled.pop(key, None)
