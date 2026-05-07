from __future__ import annotations

import os
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

from jarvis_contracts import (
    ClientAction,
    ClientActionEnvelope,
    ClientActionResult,
    ClientActionResultRequest,
)


def _default_timeout_seconds() -> float:
    raw = os.getenv("JARVIS_CLIENT_ACTION_TIMEOUT_SECONDS", "5")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


@dataclass(slots=True)
class _ActionRecord:
    user_id: str
    envelope: ClientActionEnvelope
    result: ClientActionResult | None = None


class ActionDispatcher:
    """In-memory client action queue and result handoff.

    The controller owns this because it is the public API boundary. A user-side
    runtime polls pending actions, executes them locally, then posts the result.
    """

    def __init__(self, timeout_seconds: float | None = None) -> None:
        self.timeout_seconds = (
            _default_timeout_seconds()
            if timeout_seconds is None
            else timeout_seconds
        )
        self.context_store = None
        self._condition = threading.Condition()
        self._queues: dict[str, deque[str]] = defaultdict(deque)
        self._records: dict[str, _ActionRecord] = {}

    def enqueue(
        self,
        *,
        user_id: str,
        request_id: str,
        action: ClientAction,
    ) -> ClientActionEnvelope:
        action_id = f"act_{uuid.uuid4().hex}"
        envelope = ClientActionEnvelope(
            action_id=action_id,
            request_id=request_id,
            action=action,
        )
        with self._condition:
            self._records[action_id] = _ActionRecord(
                user_id=user_id,
                envelope=envelope,
            )
            self._queues[user_id].append(action_id)
            self._condition.notify_all()
        return envelope

    def pending(self, *, user_id: str, limit: int = 20) -> list[ClientActionEnvelope]:
        with self._condition:
            queue = self._queues[user_id]
            envelopes: list[ClientActionEnvelope] = []
            while queue and len(envelopes) < limit:
                action_id = queue.popleft()
                record = self._records.get(action_id)
                if record is not None and record.result is None:
                    envelopes.append(record.envelope)
            return envelopes

    def complete(
        self,
        *,
        user_id: str,
        action_id: str,
        body: ClientActionResultRequest,
    ) -> ClientActionResult | None:
        with self._condition:
            record = self._records.get(action_id)
            if record is None or record.user_id != user_id:
                return None
            result = ClientActionResult(
                action_id=action_id,
                request_id=record.envelope.request_id,
                status=body.status,
                output=body.output,
                error=body.error,
            )
            record.result = result
            if self.context_store is not None:
                self.context_store.record_action_result(
                    user_id=user_id,
                    action=record.envelope.action,
                    status=result.status,
                    output=result.output,
                    action_id=action_id,
                )
            self._condition.notify_all()
            return result

    def dispatch_and_wait(
        self,
        *,
        user_id: str,
        request_id: str,
        action: ClientAction,
        timeout_seconds: float | None = None,
    ) -> tuple[ClientActionEnvelope, ClientActionResult]:
        envelope = self.enqueue(user_id=user_id, request_id=request_id, action=action)
        return envelope, self.wait_for_result(
            action_id=envelope.action_id,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )

    def wait_for_result(
        self,
        *,
        action_id: str,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> ClientActionResult:
        deadline = time.monotonic() + (
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        with self._condition:
            while True:
                record = self._records.get(action_id)
                if record is not None and record.result is not None:
                    return record.result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result = ClientActionResult(
                        action_id=action_id,
                        request_id=request_id,
                        status="timeout",
                        error="client action result timed out",
                    )
                    if record is not None:
                        record.result = result
                    return result
                self._condition.wait(timeout=remaining)
