from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from jarvis_contracts import ClientAction

RECENT_ACTION_LIMIT = 8
SEEN_ACTION_ID_LIMIT = 64
MAX_TYPED_TEXT_CHARS = 2000
TRUNCATED_MARKER = "...[truncated]"


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
    action_id: str | None = None
    target: str | None = None
    description: str | None = None
    updated_at: float = 0.0


@dataclass
class RecentActionContext:
    action_id: str | None
    action_type: str
    command: str | None
    target: str | None
    status: str
    description: str | None
    args: dict[str, Any] = field(default_factory=dict)
    text_summary: str | None = None
    updated_at: float = 0.0

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action_type": self.action_type,
            "command": self.command,
            "target": self.target,
            "status": self.status,
            "description": self.description,
            "args": dict(self.args),
        }
        if self.action_id:
            data["action_id"] = self.action_id
        if self.text_summary:
            data["text_summary"] = self.text_summary
        return data


@dataclass
class WorkingActionContext:
    active_surface: str | None = None
    active_app: str | None = None
    active_browser: str | None = None
    recent_actions: list[RecentActionContext] = field(default_factory=list)
    latest_result: ActionResultContext | None = None
    latest_observation: ActionResultContext | None = None
    last_typed_text: str | None = None
    last_typed_target: str | None = None
    last_user_visible_output: str | None = None
    updated_at: float = 0.0

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "active_surface": self.active_surface,
            "active_app": self.active_app,
            "active_browser": self.active_browser,
            "recent_actions": [item.payload() for item in self.recent_actions],
            "last_typed_text": self.last_typed_text,
            "last_typed_target": self.last_typed_target,
            "last_user_visible_output": self.last_user_visible_output,
        }
        if self.latest_result is not None:
            data["latest_result"] = _result_payload(self.latest_result)
        if self.latest_observation is not None:
            data["latest_observation"] = _result_payload(self.latest_observation)
        return {key: value for key, value in data.items() if value not in (None, [], {})}


class ActionContextStore:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._browser: dict[str, BrowserContext] = {}
        self._latest_results: dict[str, ActionResultContext] = {}
        self._latest_observations: dict[str, ActionResultContext] = {}
        self._working: dict[str, WorkingActionContext] = {}
        self._seen_action_ids: dict[str, list[str]] = {}

    def record_action_result(
        self,
        *,
        user_id: str,
        action: ClientAction,
        status: str,
        output: dict[str, Any],
        action_id: str | None = None,
    ) -> None:
        if action_id and not self._mark_seen(user_id, action_id):
            return
        now = time.monotonic()
        latest_result = ActionResultContext(
            action_type=action.type,
            command=action.command,
            status=status,
            output=dict(output) if isinstance(output, dict) else {},
            action_id=action_id,
            target=action.target,
            description=action.description,
            updated_at=now,
        )
        self._latest_results[user_id] = latest_result
        working = self._working_context_for_update(user_id, now)
        working.latest_result = latest_result
        working.updated_at = now
        if action.type == "screenshot":
            self._latest_observations[user_id] = latest_result
            working.latest_observation = latest_result
        self._record_recent_action(
            working,
            action=action,
            status=status,
            action_id=action_id,
            now=now,
        )
        self._record_visible_output(working, action=action, output=output)
        if status != "completed":
            return
        self._record_completed_context(user_id, working, action, output, now)

    def browser_context(self, user_id: str) -> BrowserContext | None:
        return self._fresh(self._browser, user_id)

    def latest_result(self, user_id: str) -> ActionResultContext | None:
        return self._fresh(self._latest_results, user_id)

    def latest_observation(self, user_id: str) -> ActionResultContext | None:
        return self._fresh(self._latest_observations, user_id)

    def working_context(self, user_id: str) -> dict[str, Any] | None:
        context = self._fresh(self._working, user_id)
        if context is None:
            return None
        return context.payload()

    def _working_context_for_update(
        self,
        user_id: str,
        now: float,
    ) -> WorkingActionContext:
        context = self._fresh(self._working, user_id)
        if context is None:
            context = WorkingActionContext(updated_at=now)
            self._working[user_id] = context
        return context

    def _record_completed_context(
        self,
        user_id: str,
        working: WorkingActionContext,
        action: ClientAction,
        output: dict[str, Any],
        now: float,
    ) -> None:
        if action.type == "app_control":
            app_name = _app_target(action)
            if app_name:
                working.active_surface = "app"
                working.active_app = app_name
            return

        if action.type == "keyboard_type":
            text = _typed_text(action)
            if text:
                working.active_surface = working.active_surface or "app"
                working.last_typed_text = _truncate_text(text, MAX_TYPED_TEXT_CHARS)
                working.last_typed_target = working.active_app or working.active_browser
            return

        if action.type not in {"open_url", "browser_control"}:
            return

        opened = _browser_location_from_output(output) or str(action.target or "")
        if not opened:
            return
        query = _query_from_action(action)
        self._browser[user_id] = BrowserContext(
            last_query=query,
            last_url=opened,
            updated_at=now,
        )
        working.active_surface = "browser"
        working.active_browser = opened

    def _record_recent_action(
        self,
        working: WorkingActionContext,
        *,
        action: ClientAction,
        status: str,
        action_id: str | None,
        now: float,
    ) -> None:
        if action_id:
            working.recent_actions = [
                item for item in working.recent_actions if item.action_id != action_id
            ]
        text = _typed_text(action) if action.type == "keyboard_type" else None
        working.recent_actions.insert(
            0,
            RecentActionContext(
                action_id=action_id,
                action_type=action.type,
                command=action.command,
                target=action.target,
                status=status,
                description=action.description,
                args=dict(action.args) if isinstance(action.args, dict) else {},
                text_summary=_truncate_text(text, 160) if text else None,
                updated_at=now,
            ),
        )
        del working.recent_actions[RECENT_ACTION_LIMIT:]

    def _record_visible_output(
        self,
        working: WorkingActionContext,
        *,
        action: ClientAction,
        output: dict[str, Any],
    ) -> None:
        if action.type == "keyboard_type":
            text = _typed_text(action)
            if text:
                working.last_user_visible_output = _truncate_text(text, 500)
            return
        visible = _visible_output_from_result(output)
        if visible:
            working.last_user_visible_output = _truncate_text(visible, 500)

    def _mark_seen(self, user_id: str, action_id: str) -> bool:
        seen = self._seen_action_ids.setdefault(user_id, [])
        if action_id in seen:
            return False
        seen.append(action_id)
        del seen[:-SEEN_ACTION_ID_LIMIT]
        return True

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


def _result_payload(value: ActionResultContext) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_type": value.action_type,
        "command": value.command,
        "target": value.target,
        "status": value.status,
        "output": value.output,
    }
    if value.action_id:
        payload["action_id"] = value.action_id
    if value.description:
        payload["description"] = value.description
    return {key: item for key, item in payload.items() if item is not None}


def _app_target(action: ClientAction) -> str | None:
    if isinstance(action.target, str) and action.target.strip():
        return action.target.strip()
    if isinstance(action.command, str):
        command = action.command.strip()
        if command and command not in {"open", "focus", "close"}:
            return command
    args = action.args if isinstance(action.args, dict) else {}
    for key in ("app_name", "application", "application_name"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _typed_text(action: ClientAction) -> str | None:
    if isinstance(action.payload, str) and action.payload:
        return action.payload
    args = action.args if isinstance(action.args, dict) else {}
    value = args.get("text")
    if isinstance(value, str) and value:
        return value
    return None


def _query_from_action(action: ClientAction) -> str | None:
    if isinstance(action.args, dict):
        raw_query = action.args.get("query")
        if isinstance(raw_query, str) and raw_query.strip():
            return raw_query.strip()
    if action.type == "browser_control" and action.command == "select_result":
        return None
    return None


def _visible_output_from_result(output: dict[str, Any]) -> str | None:
    for key in ("message", "summary", "text", "title", "opened", "url", "href"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = TRUNCATED_MARKER
    return value[: max(0, limit - len(marker))].rstrip() + marker
