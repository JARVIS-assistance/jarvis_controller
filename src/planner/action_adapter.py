from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from jarvis_contracts import (
    ClientAction,
    ClientActionPlan,
    ClientActionV2,
    ClientActionValidationIssue,
)

from planner.action_validator import ActionValidator


@dataclass(frozen=True)
class ActionAdapterResult:
    valid: bool
    actions: list[ClientAction]
    issues: list[ClientActionValidationIssue]


class V2ToV1ActionAdapter:
    """Deterministically adapt validated v2 actions to existing v1 handlers."""

    def __init__(self, validator: ActionValidator | None = None) -> None:
        self.validator = validator or ActionValidator()

    def adapt_plan(
        self,
        plan: ClientActionPlan,
        *,
        context: dict[str, Any] | None = None,
    ) -> ActionAdapterResult:
        validation = self.validator.validate_plan(plan, context=context)
        if not validation.valid or validation.plan is None:
            return ActionAdapterResult(False, [], validation.issues)
        if validation.plan.mode == "no_action":
            return ActionAdapterResult(True, [], [])

        actions: list[ClientAction] = []
        issues: list[ClientActionValidationIssue] = []
        for index, action in enumerate(validation.plan.actions):
            adapted = self._adapt_action(action, index=index, context=context)
            if isinstance(adapted, ClientActionValidationIssue):
                issues.append(adapted)
            else:
                actions.append(adapted)
        return ActionAdapterResult(not issues, actions, issues)

    def _adapt_action(
        self,
        action: ClientActionV2,
        *,
        index: int,
        context: dict[str, Any] | None,
    ) -> ClientAction | ClientActionValidationIssue:
        args = action.args if isinstance(action.args, dict) else {}
        browser = _string_arg(args, "browser") or _context_string(context, "default_browser")

        if action.name == "browser.search":
            query = _string_arg(args, "query")
            if query is None:
                return _issue("missing_query", "browser.search requires args.query", index, action)
            url = _search_url(query, context=context)
            return ClientAction(
                type="open_url",
                command=None,
                target=url,
                args=_without_none({"browser": browser, "query": query}),
                description=action.description or f"Search browser for {query}",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "browser.navigate":
            url = _string_arg(args, "url")
            if url is None:
                return _issue("missing_url", "browser.navigate requires args.url", index, action)
            return ClientAction(
                type="open_url",
                command=None,
                target=url,
                args=_without_none({"browser": browser}),
                description=action.description or f"Navigate browser to {url}",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "browser.open":
            url = _string_arg(args, "url")
            if url:
                return ClientAction(
                    type="open_url",
                    command=None,
                    target=url,
                    args=_without_none({"browser": browser}),
                    description=action.description or f"Open browser at {url}",
                    requires_confirm=action.requires_confirm,
                    step_id=action.step_id,
                )
            return ClientAction(
                type="browser",
                command="open",
                target=None,
                args=_without_none({"browser": browser}),
                description=action.description or "Open browser",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "browser.extract_dom":
            return ClientAction(
                type="browser_control",
                command="extract_dom",
                target=action.target or "active_tab",
                args=dict(args),
                description=action.description or "Extract browser DOM",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "browser.click":
            return ClientAction(
                type="browser_control",
                command="click_element",
                target=action.target or "active_tab",
                args={"ai_id": args.get("ai_id")},
                description=action.description or "Click browser element",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "browser.type":
            text = _string_arg(args, "text") or action.payload
            adapted_args = {"ai_id": args.get("ai_id"), "enter": bool(args.get("enter", False))}
            return ClientAction(
                type="browser_control",
                command="type_element",
                target=action.target or "active_tab",
                payload=text,
                args=adapted_args,
                description=action.description or "Type into browser element",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "browser.select_result":
            return ClientAction(
                type="browser_control",
                command="select_result",
                target=action.target or "active_tab",
                args={"index": args.get("index")},
                description=action.description or "Open browser search result",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name in {"app.open", "app.focus"}:
            command = "open" if action.name == "app.open" else "focus"
            return ClientAction(
                type="app_control",
                command=command,
                target=action.target,
                args=dict(args),
                description=action.description or f"{command} {action.target}",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "keyboard.type":
            text = _string_arg(args, "text") or action.payload
            return ClientAction(
                type="keyboard_type",
                command=None,
                target=action.target,
                payload=text,
                args={"enter": bool(args.get("enter", False))},
                description=action.description or "Type text",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "keyboard.hotkey":
            return ClientAction(
                type="hotkey",
                command=None,
                target=action.target,
                args={"keys": args.get("keys")},
                description=action.description or "Press hotkey",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "mouse.click":
            return ClientAction(
                type="mouse_click",
                command=None,
                target=action.target,
                args=dict(args),
                description=action.description or "Mouse click",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "mouse.drag":
            return ClientAction(
                type="mouse_drag",
                command=None,
                target=action.target,
                args=dict(args),
                description=action.description or "Mouse drag",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "screen.screenshot":
            return ClientAction(
                type="screenshot",
                command=None,
                target=action.target,
                args=dict(args),
                description=action.description or "Capture screenshot",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name in {"clipboard.copy", "clipboard.paste"}:
            command = "copy" if action.name == "clipboard.copy" else "paste"
            return ClientAction(
                type="clipboard",
                command=command,
                target=action.target,
                payload=_string_arg(args, "text") or action.payload,
                args={},
                description=action.description or f"Clipboard {command}",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name == "terminal.run":
            command_text = _string_arg(args, "command") or action.payload
            terminal_args = dict(args)
            terminal_args.pop("command", None)
            return ClientAction(
                type="terminal",
                command="execute",
                target=action.target or _context_string(context, "shell"),
                payload=command_text,
                args=terminal_args,
                description=action.description or "Run terminal command",
                requires_confirm=True,
                step_id=action.step_id,
            )

        if action.name == "notification.show":
            return ClientAction(
                type="notify",
                command=None,
                target=action.target,
                payload=_string_arg(args, "text") or action.payload,
                args={key: value for key, value in args.items() if key != "text"},
                description=action.description or "Show notification",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        if action.name.startswith("calendar."):
            command = {
                "calendar.open": "open",
                "calendar.create": "create_event",
                "calendar.update": "update_event",
                "calendar.delete": "delete_event",
            }[action.name]
            return ClientAction(
                type="calendar_control",
                command=command,
                target=action.target,
                args=dict(args),
                description=action.description or f"Calendar {command}",
                requires_confirm=action.requires_confirm,
                step_id=action.step_id,
            )

        return _issue("unsupported_action", f"Unsupported action: {action.name}", index, action)


def _search_url(query: str, *, context: dict[str, Any] | None) -> str:
    template = _context_string(context, "search_engine_url_template")
    if not template:
        search_engine = (context or {}).get("search_engine")
        if isinstance(search_engine, dict):
            template = _string_value(search_engine.get("url_template"))
        elif isinstance(search_engine, str):
            template = _search_engine_template(search_engine)
    encoded = urllib.parse.quote_plus(query)
    if template and "{query}" in template:
        return template.replace("{query}", encoded)
    return "https://www.google.com/search?q=" + encoded


def _search_engine_template(search_engine: str) -> str | None:
    normalized = search_engine.strip().lower().replace("-", "")
    if normalized == "naver":
        return "https://search.naver.com/search.naver?query={query}"
    if normalized == "duckduckgo":
        return "https://duckduckgo.com/?q={query}"
    if normalized == "google":
        return "https://www.google.com/search?q={query}"
    return None


def _string_arg(args: dict[str, Any], key: str) -> str | None:
    return _string_value(args.get(key))


def _context_string(context: dict[str, Any] | None, key: str) -> str | None:
    return _string_value((context or {}).get(key))


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _issue(
    code: str,
    message: str,
    index: int,
    action: ClientActionV2,
    *,
    field: str | None = None,
) -> ClientActionValidationIssue:
    return ClientActionValidationIssue(
        code=code,
        message=message,
        action_index=index,
        action_name=action.name,
        field=field,
    )
