from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis_contracts import (
    ACTION_V2_CAPABILITIES,
    COMMANDS_BY_ACTION_TYPE,
    ClientAction,
    ClientActionPlan,
    ClientActionV2,
    ClientActionValidationIssue,
)

RISKY_V2_ACTIONS = {
    "terminal.run",
    "calendar.create",
    "calendar.update",
    "calendar.delete",
}

RISKY_V1_ACTIONS = {
    ("terminal", "execute"),
    ("calendar_control", "create_event"),
    ("calendar_control", "update_event"),
    ("calendar_control", "delete_event"),
}

ABSTRACT_APP_TARGETS = {
    "browser",
    "defaultbrowser",
    "default_browser",
    "webbrowser",
    "web_browser",
    "browserapp",
    "web",
}


@dataclass(frozen=True)
class ActionValidationResult:
    valid: bool
    plan: ClientActionPlan | None
    issues: list[ClientActionValidationIssue]


@dataclass(frozen=True)
class V1ActionValidationResult:
    valid: bool
    actions: list[ClientAction]
    issues: list[ClientActionValidationIssue]


class ActionValidator:
    """Validate structured action contracts without inferring user intent."""

    def validate_plan(
        self,
        plan: ClientActionPlan,
        *,
        context: dict[str, Any] | None = None,
    ) -> ActionValidationResult:
        normalized = plan.model_copy(deep=True)
        issues: list[ClientActionValidationIssue] = []

        if normalized.mode == "no_action":
            if normalized.actions:
                issues.append(
                    _issue(
                        "no_action_has_actions",
                        "no_action plans must not contain actions",
                        field="actions",
                    )
                )
            return ActionValidationResult(not issues, normalized, issues)

        if normalized.mode in {"direct", "direct_sequence"} and not normalized.actions:
            issues.append(
                _issue(
                    "missing_actions",
                    "direct action plans must contain at least one action",
                    field="actions",
                )
            )

        for index, action in enumerate(normalized.actions):
            issues.extend(self._validate_v2_action(action, index=index, context=context))

        return ActionValidationResult(not issues, normalized, issues)

    def validate_v1_actions(
        self,
        actions: list[ClientAction],
        *,
        context: dict[str, Any] | None = None,
    ) -> V1ActionValidationResult:
        normalized = [action.model_copy(deep=True) for action in actions]
        issues: list[ClientActionValidationIssue] = []
        for index, action in enumerate(normalized):
            issues.extend(self._validate_v1_action(action, index=index, context=context))
        return V1ActionValidationResult(not issues, normalized, issues)

    def _validate_v2_action(
        self,
        action: ClientActionV2,
        *,
        index: int,
        context: dict[str, Any] | None,
    ) -> list[ClientActionValidationIssue]:
        issues: list[ClientActionValidationIssue] = []
        spec = ACTION_V2_CAPABILITIES.get(action.name)
        if spec is None:
            return [
                _issue(
                    "unknown_action",
                    f"Unknown v2 action capability: {action.name}",
                    action_index=index,
                    action_name=action.name,
                    field="name",
                )
            ]

        if not _capability_enabled(action.name, context):
            issues.append(
                _issue(
                    "disabled_capability",
                    f"Capability is not enabled: {action.name}",
                    action_index=index,
                    action_name=action.name,
                    field="name",
                )
            )

        if action.name in {"app.open", "app.focus"} and _abstract_app_target(action.target):
            issues.append(
                _issue(
                    "abstract_app_target",
                    "app.open/app.focus require a concrete application target",
                    action_index=index,
                    action_name=action.name,
                    field="target",
                    details={"target": action.target},
                )
            )

        issues.extend(_required_v2_args(action, index=index))

        if action.name in RISKY_V2_ACTIONS:
            action.requires_confirm = True

        return issues

    def _validate_v1_action(
        self,
        action: ClientAction,
        *,
        index: int,
        context: dict[str, Any] | None,
    ) -> list[ClientActionValidationIssue]:
        issues: list[ClientActionValidationIssue] = []
        allowed_commands = COMMANDS_BY_ACTION_TYPE.get(action.type)
        if allowed_commands is None or action.command not in allowed_commands:
            issues.append(
                _issue(
                    "invalid_v1_command",
                    f"Invalid v1 action command: {action.type}/{action.command}",
                    action_index=index,
                    action_name=action.type,
                    field="command",
                )
            )

        if not _v1_capability_enabled(action.type, action.command, context):
            issues.append(
                _issue(
                    "disabled_capability",
                    f"Capability is not enabled: {action.type}/{action.command}",
                    action_index=index,
                    action_name=action.type,
                    field="type",
                )
            )

        if action.type == "app_control" and action.command == "open":
            if _abstract_app_target(action.target):
                issues.append(
                    _issue(
                        "abstract_app_target",
                        "app_control/open requires a concrete application target",
                        action_index=index,
                        action_name=action.type,
                        field="target",
                        details={"target": action.target},
                    )
                )

        if (action.type, action.command) in RISKY_V1_ACTIONS:
            action.requires_confirm = True

        return issues


def _required_v2_args(
    action: ClientActionV2,
    *,
    index: int,
) -> list[ClientActionValidationIssue]:
    args = action.args if isinstance(action.args, dict) else {}
    issues: list[ClientActionValidationIssue] = []

    def require_string(field: str, *, source: dict[str, Any] = args) -> None:
        value = source.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(
                _issue(
                    "missing_required_field",
                    f"{action.name} requires {field}",
                    action_index=index,
                    action_name=action.name,
                    field=f"args.{field}",
                )
            )

    if action.name == "browser.navigate":
        require_string("url")
    elif action.name == "browser.search":
        require_string("query")
    elif action.name == "browser.click":
        if not isinstance(args.get("ai_id"), int):
            issues.append(
                _issue(
                    "missing_required_field",
                    "browser.click requires args.ai_id",
                    action_index=index,
                    action_name=action.name,
                    field="args.ai_id",
                )
            )
    elif action.name == "browser.type":
        if not isinstance(args.get("ai_id"), int):
            issues.append(
                _issue(
                    "missing_required_field",
                    "browser.type requires args.ai_id",
                    action_index=index,
                    action_name=action.name,
                    field="args.ai_id",
                )
            )
        text = args.get("text") or action.payload
        if not isinstance(text, str) or not text:
            issues.append(
                _issue(
                    "missing_required_field",
                    "browser.type requires text",
                    action_index=index,
                    action_name=action.name,
                    field="args.text",
                )
            )
    elif action.name == "browser.select_result":
        if not isinstance(args.get("index"), int):
            issues.append(
                _issue(
                    "missing_required_field",
                    "browser.select_result requires args.index",
                    action_index=index,
                    action_name=action.name,
                    field="args.index",
                )
            )
    elif action.name in {"app.open", "app.focus"}:
        if not isinstance(action.target, str) or not action.target.strip():
            issues.append(
                _issue(
                    "missing_required_field",
                    f"{action.name} requires target",
                    action_index=index,
                    action_name=action.name,
                    field="target",
                )
            )
    elif action.name == "keyboard.type":
        text = args.get("text") or action.payload
        if not isinstance(text, str) or not text:
            issues.append(
                _issue(
                    "missing_required_field",
                    "keyboard.type requires text",
                    action_index=index,
                    action_name=action.name,
                    field="args.text",
                )
            )
    elif action.name == "keyboard.hotkey":
        require_string("keys")
    elif action.name == "mouse.click":
        for field in ("x", "y"):
            if not isinstance(args.get(field), int | float):
                issues.append(
                    _issue(
                        "missing_required_field",
                        f"mouse.click requires {field}",
                        action_index=index,
                        action_name=action.name,
                        field=f"args.{field}",
                    )
                )
    elif action.name == "mouse.drag":
        for field in ("start_x", "start_y", "end_x", "end_y"):
            if not isinstance(args.get(field), int | float):
                issues.append(
                    _issue(
                        "missing_required_field",
                        f"mouse.drag requires {field}",
                        action_index=index,
                        action_name=action.name,
                        field=f"args.{field}",
                    )
                )
    elif action.name == "clipboard.copy":
        text = args.get("text") or action.payload
        if not isinstance(text, str) or not text:
            issues.append(
                _issue(
                    "missing_required_field",
                    "clipboard.copy requires text",
                    action_index=index,
                    action_name=action.name,
                    field="payload",
                )
            )
    elif action.name == "terminal.run":
        command = args.get("command") or action.payload
        if not isinstance(command, str) or not command.strip():
            issues.append(
                _issue(
                    "missing_required_field",
                    "terminal.run requires command",
                    action_index=index,
                    action_name=action.name,
                    field="args.command",
                )
            )
    elif action.name == "calendar.create":
        for field in ("title", "start", "end"):
            require_string(field)
    elif action.name in {"calendar.update", "calendar.delete"}:
        require_string("event_id")

    return issues


def _capability_enabled(name: str, context: dict[str, Any] | None) -> bool:
    capability_map = _capability_map(context)
    if not capability_map:
        return True
    for candidate in _capability_candidates(name):
        if capability_map.get(candidate) is True:
            return True
    for candidate in _capability_candidates(name):
        if capability_map.get(candidate) is False:
            return False
    return False


def _v1_capability_enabled(
    action_type: str,
    command: str | None,
    context: dict[str, Any] | None,
) -> bool:
    capability_map = _capability_map(context)
    if not capability_map:
        return True
    candidates = _v1_capability_candidates(action_type, command)
    if any(capability_map.get(candidate) is True for candidate in candidates):
        return True
    if any(capability_map.get(candidate) is False for candidate in candidates):
        return False
    return False


def _capability_candidates(name: str) -> tuple[str, ...]:
    namespace = name.split(".", 1)[0]
    legacy = {
        "browser": ("browser_control", "open_url"),
        "app": ("app_control",),
        "keyboard": ("keyboard_type", "hotkey"),
        "mouse": ("mouse_click", "mouse_drag"),
        "screen": ("screenshot",),
        "clipboard": ("clipboard",),
        "terminal": ("terminal",),
        "notification": ("notify", "notification"),
        "calendar": ("calendar_control", "calendar"),
    }
    return (name, namespace, *legacy.get(namespace, ()))


def _v1_capability_candidates(
    action_type: str,
    command: str | None,
) -> tuple[str, ...]:
    mapped = {
        "open_url": ("browser", "browser.open", "browser.navigate", "open_url"),
        "browser_control": ("browser", "browser_control"),
        "app_control": ("app", f"app.{command}", "app_control"),
        "keyboard_type": ("keyboard", "keyboard.type", "keyboard_type"),
        "hotkey": ("keyboard", "keyboard.hotkey", "hotkey"),
        "mouse_click": ("mouse", "mouse.click", "mouse_click"),
        "mouse_drag": ("mouse", "mouse.drag", "mouse_drag"),
        "screenshot": ("screen", "screen.screenshot", "screenshot"),
        "clipboard": ("clipboard", f"clipboard.{command}", "clipboard"),
        "terminal": ("terminal", "terminal.run", "terminal"),
        "notify": ("notification", "notification.show", "notify"),
        "calendar_control": ("calendar", "calendar_control"),
    }
    return mapped.get(action_type, (action_type,))


def _capability_map(context: dict[str, Any] | None) -> dict[str, bool]:
    capabilities = (context or {}).get("capabilities")
    result: dict[str, bool] = {}
    if isinstance(capabilities, dict):
        for key, value in capabilities.items():
            if isinstance(key, str):
                result[key] = _capability_value_enabled(value)
    elif isinstance(capabilities, list):
        for item in capabilities:
            if isinstance(item, str):
                result[item] = True
            elif isinstance(item, dict):
                name = item.get("name") or item.get("capability") or item.get("id")
                if isinstance(name, str):
                    result[name] = _capability_value_enabled(item)
    return result


def _capability_value_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return value.get("enabled", True) is not False
    return bool(value)


def _abstract_app_target(target: str | None) -> bool:
    if not isinstance(target, str):
        return False
    normalized = "".join(ch for ch in target.lower() if ch.isalnum() or ch == "_")
    return normalized in ABSTRACT_APP_TARGETS


def _issue(
    code: str,
    message: str,
    *,
    action_index: int | None = None,
    action_name: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> ClientActionValidationIssue:
    return ClientActionValidationIssue(
        code=code,
        message=message,
        action_index=action_index,
        action_name=action_name,
        field=field,
        details=details or {},
    )
