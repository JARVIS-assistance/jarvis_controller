from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from jarvis_contracts import ClientAction


@dataclass
class BrowserContext:
    last_query: str | None = None
    last_url: str | None = None
    updated_at: float = 0.0


@dataclass
class ActionResultContext:
    action_type: str
    command: str | None
    status: str
    output: dict[str, Any]
    updated_at: float = 0.0


class ActionContextStore:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._browser: dict[str, BrowserContext] = {}
        self._latest_results: dict[str, ActionResultContext] = {}
        self._latest_observations: dict[str, ActionResultContext] = {}

    def record_action_result(
        self,
        *,
        user_id: str,
        action: ClientAction,
        status: str,
        output: dict[str, Any],
    ) -> None:
        now = time.monotonic()
        self._latest_results[user_id] = ActionResultContext(
            action_type=action.type,
            command=action.command,
            status=status,
            output=dict(output) if isinstance(output, dict) else {},
            updated_at=now,
        )
        if action.type == "screenshot":
            self._latest_observations[user_id] = self._latest_results[user_id]
        if status != "completed":
            return
        if action.type not in {"open_url", "browser_control"}:
            return
        opened = _browser_location_from_output(output) or str(action.target or "")
        if not opened:
            return
        query = None
        if isinstance(action.args, dict):
            raw_query = action.args.get("query")
            if isinstance(raw_query, str) and raw_query.strip():
                query = raw_query.strip()
        self._browser[user_id] = BrowserContext(
            last_query=query,
            last_url=opened,
            updated_at=now,
        )

    def browser_context(self, user_id: str) -> BrowserContext | None:
        return self._fresh(self._browser, user_id)

    def latest_result(self, user_id: str) -> ActionResultContext | None:
        return self._fresh(self._latest_results, user_id)

    def latest_observation(self, user_id: str) -> ActionResultContext | None:
        return self._fresh(self._latest_observations, user_id)

    def _fresh(self, mapping: dict[str, Any], user_id: str) -> Any | None:
        context = mapping.get(user_id)
        if context is None:
            return None
        if time.monotonic() - context.updated_at > self.ttl_seconds:
            mapping.pop(user_id, None)
            return None
        return context


def _browser_location_from_output(output: dict[str, Any]) -> str | None:
    for key in ("opened", "url", "href", "current_url", "location"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
