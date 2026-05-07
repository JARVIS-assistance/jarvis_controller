from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jarvis_contracts import ClientActionPlan, ClientActionValidationIssue
from pydantic import ValidationError

from planner.action_gate import ActionIntentGate


@dataclass(frozen=True)
class TemplateMaterialization:
    plan: ClientActionPlan | None = None
    issues: list[ClientActionValidationIssue] = field(default_factory=list)


def fast_action_templates() -> dict[str, Any]:
    return {
        "browser_open": {
            "mode": "direct",
            "goal": "Open browser",
            "confidence": 0.9,
            "reason": "open browser",
            "actions": [
                {
                    "name": "browser.open",
                    "args": {},
                    "description": "Open browser",
                    "requires_confirm": False,
                }
            ],
        },
        "browser_search": {
            "mode": "direct",
            "goal": "Search browser",
            "confidence": 0.9,
            "reason": "browser search",
            "actions": [
                {
                    "name": "browser.search",
                    "args": {"query": "<query>"},
                    "description": "Search browser",
                    "requires_confirm": False,
                }
            ],
        },
        "browser_search_open_first": {
            "mode": "direct_sequence",
            "goal": "Search and open first result",
            "confidence": 0.9,
            "reason": "search and open result",
            "actions": [
                {
                    "name": "browser.search",
                    "args": {"query": "<query>"},
                    "description": "Search browser",
                    "requires_confirm": False,
                },
                {
                    "name": "browser.select_result",
                    "args": {"index": 1},
                    "description": "Open first result",
                    "requires_confirm": False,
                },
            ],
        },
        "app_open": {
            "mode": "direct",
            "goal": "Open app",
            "confidence": 0.9,
            "reason": "open local app",
            "actions": [
                {
                    "name": "app.open",
                    "target": "<exact app name>",
                    "args": {},
                    "description": "Open app",
                    "requires_confirm": False,
                }
            ],
        },
        "app_open_type": {
            "mode": "direct_sequence",
            "goal": "Open app and type text",
            "confidence": 0.9,
            "reason": "open local app and type",
            "actions": [
                {
                    "name": "app.open",
                    "target": "<exact app name>",
                    "args": {},
                    "description": "Open app",
                    "requires_confirm": False,
                },
                {
                    "name": "keyboard.type",
                    "args": {"text": "<text>"},
                    "description": "Type text",
                    "requires_confirm": False,
                },
            ],
        },
        "screen_screenshot": {
            "mode": "direct",
            "goal": "Capture current screen",
            "confidence": 0.9,
            "reason": "screen capture request",
            "actions": [
                {
                    "name": "screen.screenshot",
                    "args": {},
                    "description": "Capture current screen",
                    "requires_confirm": False,
                }
            ],
        },
    }


_TEMPLATE_KEY_ALIASES = {
    "browser_open": "browser_open",
    "browser.open": "browser_open",
    "browser_search": "browser_search",
    "browser.search": "browser_search",
    "browser_search_open_first": "browser_search_open_first",
    "browser.search_open_first": "browser_search_open_first",
    "browser.search.open_first": "browser_search_open_first",
    "browser.search+browser.select_result": "browser_search_open_first",
    "app_open": "app_open",
    "app.open": "app_open",
    "app_open_type": "app_open_type",
    "app.open_type": "app_open_type",
    "app.open+keyboard.type": "app_open_type",
    "app.open+keyboard_type": "app_open_type",
    "screen_screenshot": "screen_screenshot",
    "screen.screenshot": "screen_screenshot",
    "screenshot": "screen_screenshot",
}


def materialize_gate_template(
    gate: ActionIntentGate,
    *,
    context: dict[str, Any] | None,
) -> TemplateMaterialization:
    template_key = template_key_for_gate(gate)
    if template_key is None:
        return TemplateMaterialization()

    template = fast_action_templates().get(template_key)
    if template is None:
        return TemplateMaterialization(
            issues=[
                _issue(
                    "unsupported_intent_template",
                    f"Unsupported intent template: {template_key}",
                    field="template_key",
                    details={"template_key": template_key, "intent": gate.intent},
                )
            ]
        )

    payload = json.loads(json.dumps(template, ensure_ascii=False))
    payload["confidence"] = max(float(payload.get("confidence") or 0), gate.confidence)
    payload["reason"] = gate.reason or payload.get("reason")
    slots = gate.slots or {}
    issues: list[ClientActionValidationIssue] = []

    if template_key == "browser_search":
        query = _slot_string(slots, "query", "search_query", "terms")
        if query is None:
            issues.append(_missing_template_slot("slots.query", template_key, gate))
        else:
            payload["actions"][0]["args"]["query"] = query

    elif template_key == "browser_search_open_first":
        query = _slot_string(slots, "query", "search_query", "terms")
        if query is None:
            issues.append(_missing_template_slot("slots.query", template_key, gate))
        else:
            payload["actions"][0]["args"]["query"] = query
        result_index = _slot_positive_int(
            slots,
            "open_result_index",
            "result_index",
            "index",
            default=1,
        )
        if result_index is None:
            issues.append(
                _missing_template_slot("slots.open_result_index", template_key, gate)
            )
        else:
            payload["actions"][1]["args"]["index"] = result_index

    elif template_key == "app_open":
        app_name = _slot_string(
            slots,
            "app_name",
            "application",
            "application_name",
            "exact_app_name",
            "target",
        ) or _working_context_string(context, "active_app")
        if app_name is None:
            issues.append(_missing_template_slot("slots.app_name", template_key, gate))
        else:
            payload["actions"][0]["target"] = _canonical_application_target(
                app_name,
                context,
            )

    elif template_key == "app_open_type":
        app_name = _slot_string(
            slots,
            "app_name",
            "application",
            "application_name",
            "exact_app_name",
            "target",
        ) or _working_context_string(context, "active_app")
        text = _slot_string(slots, "text", "input_text", "typed_text", "value")
        if app_name is None:
            issues.append(_missing_template_slot("slots.app_name", template_key, gate))
        else:
            payload["actions"][0]["target"] = _canonical_application_target(
                app_name,
                context,
            )
        if text is None:
            issues.append(_missing_template_slot("slots.text", template_key, gate))
        else:
            payload["actions"][1]["args"]["text"] = text

    elif template_key == "screen_screenshot":
        region = slots.get("region")
        if _valid_screenshot_region(region):
            payload["actions"][0]["args"]["region"] = region

    if _payload_has_placeholder(payload):
        issues.append(
            _issue(
                "unresolved_template_placeholder",
                "Intent template fallback still contains unresolved placeholders.",
                field="action_templates",
                details={"template_key": template_key},
            )
        )
    if issues:
        return TemplateMaterialization(issues=issues)

    try:
        return TemplateMaterialization(plan=ClientActionPlan.model_validate(payload))
    except ValidationError as exc:
        return TemplateMaterialization(
            issues=[
                _issue(
                    "invalid_intent_template_plan",
                    "Intent template fallback produced an invalid action plan.",
                    field="action_templates",
                    details={"template_key": template_key, "errors": exc.errors()},
                )
            ]
        )


def required_action_names_for_gate(gate: ActionIntentGate) -> tuple[str, ...]:
    template_key = template_key_for_gate(gate)
    if template_key is None:
        return ()
    template = fast_action_templates().get(template_key)
    if template is None:
        return ()
    actions = template.get("actions")
    if not isinstance(actions, list):
        return ()
    names: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = action.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)


def template_key_for_gate(gate: ActionIntentGate) -> str | None:
    raw = gate.template_key or gate.intent
    if not isinstance(raw, str) or not raw.strip():
        return None
    key = _TEMPLATE_KEY_ALIASES.get(raw.strip().lower())
    if (
        key == "browser_search"
        and _slot_positive_int(
            gate.slots,
            "open_result_index",
            "result_index",
            "index",
            default=None,
        )
        is not None
    ):
        return "browser_search_open_first"
    return key


def _missing_template_slot(
    field: str,
    template_key: str,
    gate: ActionIntentGate,
) -> ClientActionValidationIssue:
    return _issue(
        "missing_template_slot",
        f"Intent template {template_key} requires {field}.",
        field=field,
        details={
            "template_key": template_key,
            "intent": gate.intent,
            "slots": gate.slots,
        },
    )


def _slot_string(slots: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = slots.get(name)
        if isinstance(value, str):
            text = value.strip()
            if text and not _is_placeholder(text):
                return text
    return None


def _slot_positive_int(
    slots: dict[str, Any],
    *names: str,
    default: int | None,
) -> int | None:
    for name in names:
        value = _coerce_result_index(slots.get(name))
        if value is not None and value >= 1:
            return value
    return default


def _canonical_application_target(
    app_name: str,
    context: dict[str, Any] | None,
) -> str:
    raw = (context or {}).get("available_applications")
    if not isinstance(raw, list):
        return app_name
    requested = _application_match_key(app_name)
    if not requested:
        return app_name
    for item in raw:
        if isinstance(item, str):
            if _application_match_key(item) == requested:
                return item.strip()
            continue
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        candidates = [name]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            candidates.extend(alias for alias in aliases if isinstance(alias, str))
        if any(_application_match_key(candidate) == requested for candidate in candidates):
            return name.strip()
    return app_name


def _working_context_string(
    context: dict[str, Any] | None,
    key: str,
) -> str | None:
    working_context = (context or {}).get("working_context")
    if not isinstance(working_context, dict):
        return None
    value = working_context.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _application_match_key(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _valid_screenshot_region(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, (int, float)) for item in value)
    )


def _payload_has_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return _is_placeholder(value)
    if isinstance(value, dict):
        return any(_payload_has_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_payload_has_placeholder(item) for item in value)
    return False


def _is_placeholder(value: str) -> bool:
    text = value.strip()
    return len(text) >= 3 and text.startswith("<") and text.endswith(">")


def _coerce_result_index(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.isdigit():
        return int(text)
    match = re.search(r"(?:search[_\-\s]*result[_\-\s]*)?(\d+)", text)
    if match:
        return int(match.group(1))
    return None


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
