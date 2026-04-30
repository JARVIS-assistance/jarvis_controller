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


class ActionContextStore:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._browser: dict[str, BrowserContext] = {}

    def record_action_result(
        self,
        *,
        user_id: str,
        action: ClientAction,
        status: str,
        output: dict[str, Any],
    ) -> None:
        if status != "completed":
            return
        if action.type != "open_url":
            return
        opened = str(output.get("opened") or action.target or "")
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
            updated_at=time.monotonic(),
        )

    def browser_context(self, user_id: str) -> BrowserContext | None:
        context = self._browser.get(user_id)
        if context is None:
            return None
        if time.monotonic() - context.updated_at > self.ttl_seconds:
            self._browser.pop(user_id, None)
            return None
        return context
