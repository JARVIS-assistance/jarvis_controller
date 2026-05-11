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


def materialize_fresh_context_app_preference(
    gate: ActionIntentGate,
    *,
    context: dict[str, Any] | None,
) -> TemplateMaterialization:
    """Prefer a matching local app over browser search in fresh action contexts."""
    template_key = template_key_for_gate(gate)
    if template_key not in {"browser_search", "browser_search_open_first"}:
        return TemplateMaterialization()
    if not _is_fresh_action_context(context):
        return TemplateMaterialization()
    query = _slot_string(gate.slots or {}, "query", "search_query", "terms")
    if query is None:
        return TemplateMaterialization()
    return materialize_fresh_context_app_open_for_text(
        query,
        confidence=gate.confidence,
        context=context,
        reason="fresh action context prefers matching local app",
    )


def materialize_fresh_context_app_open_for_text(
    text: str,
    *,
    confidence: float,
    context: dict[str, Any] | None,
    reason: str,
) -> TemplateMaterialization:
    """Open a local app when fresh-context text matches runtime app metadata."""
    if not _is_fresh_action_context(context):
        return TemplateMaterialization()
    query = text.strip()
    if not query:
        return TemplateMaterialization()
    app_name = _matching_application_for_text(query, context)
    if app_name is None:
        return TemplateMaterialization()

    payload = json.loads(
        json.dumps(fast_action_templates()["app_open"], ensure_ascii=False)
    )
    payload["goal"] = f"Open {app_name}"
    payload["confidence"] = max(float(payload.get("confidence") or 0), confidence)
    payload["reason"] = reason
    payload["actions"][0]["target"] = app_name
    payload["actions"][0]["description"] = f"Open {app_name}"
    try:
        return TemplateMaterialization(plan=ClientActionPlan.model_validate(payload))
    except ValidationError as exc:
        return TemplateMaterialization(
            issues=[
                _issue(
                    "invalid_app_preference_template_plan",
                    "Fresh-context app preference produced an invalid action plan.",
                    field="action_templates",
                    details={"template_key": "app_open", "errors": exc.errors()},
                )
            ]
        )


def materialize_contextual_app_followup_search_for_text(
    text: str,
    *,
    confidence: float,
    context: dict[str, Any] | None,
    reason: str,
) -> TemplateMaterialization:
    """Search the browser for app-domain follow-ups after a local app action."""
    query = text.strip()
    if not query or not _context_supports_action(context, "browser.search"):
        return TemplateMaterialization()
    if not _text_matches_active_application(query, context):
        return TemplateMaterialization()

    template_key = (
        "browser_search_open_first"
        if _looks_like_search_open_first_request(query)
        and _context_supports_action(context, "browser.select_result")
        else "browser_search"
    )
    payload = json.loads(json.dumps(fast_action_templates()[template_key], ensure_ascii=False))
    payload["goal"] = "Search browser"
    payload["confidence"] = max(float(payload.get("confidence") or 0), confidence)
    payload["reason"] = reason
    payload["actions"][0]["args"]["query"] = query
    payload["actions"][0]["description"] = "Search browser"
    if template_key == "browser_search_open_first":
        payload["goal"] = "Search and open first result"
        payload["actions"][1]["args"]["index"] = 1
    try:
        return TemplateMaterialization(plan=ClientActionPlan.model_validate(payload))
    except ValidationError as exc:
        return TemplateMaterialization(
            issues=[
                _issue(
                    "invalid_contextual_followup_search_template_plan",
                    "Contextual app follow-up produced an invalid action plan.",
                    field="action_templates",
                    details={"template_key": template_key, "errors": exc.errors()},
                )
            ]
        )


def materialize_browser_search_for_text(
    text: str,
    *,
    confidence: float,
    context: dict[str, Any] | None,
    reason: str,
) -> TemplateMaterialization:
    """Search the browser when the user explicitly asks to search/find."""
    query = text.strip()
    if not query or not _context_supports_action(context, "browser.search"):
        return TemplateMaterialization()
    if not _looks_like_browser_search_request(query):
        return TemplateMaterialization()

    payload = json.loads(
        json.dumps(fast_action_templates()["browser_search"], ensure_ascii=False)
    )
    payload["goal"] = "Search browser"
    payload["confidence"] = max(float(payload.get("confidence") or 0), confidence)
    payload["reason"] = reason
    payload["actions"][0]["args"]["query"] = query
    payload["actions"][0]["description"] = "Search browser"
    try:
        return TemplateMaterialization(plan=ClientActionPlan.model_validate(payload))
    except ValidationError as exc:
        return TemplateMaterialization(
            issues=[
                _issue(
                    "invalid_browser_search_template_plan",
                    "Browser search fallback produced an invalid action plan.",
                    field="action_templates",
                    details={"template_key": "browser_search", "errors": exc.errors()},
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


def _is_fresh_action_context(context: dict[str, Any] | None) -> bool:
    if not isinstance(context, dict):
        return False
    for key in ("working_context", "latest_action_result", "latest_observation"):
        if context.get(key):
            return False
    return context.get("browser_active") is not True


def _matching_application_for_text(
    text: str,
    context: dict[str, Any] | None,
) -> str | None:
    raw = (context or {}).get("available_applications")
    if not isinstance(raw, list):
        return None
    text_key = _application_match_key(text)
    if not text_key:
        return None
    for item in raw:
        if isinstance(item, str):
            app_name = item.strip()
            candidates = [app_name]
        elif isinstance(item, dict):
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            app_name = name.strip()
            candidates = _application_match_candidates(item)
        else:
            continue
        for candidate in candidates:
            candidate_key = _application_match_key(candidate)
            if len(candidate_key) >= 2 and candidate_key in text_key:
                return app_name
    return None


def _text_matches_active_application(
    text: str,
    context: dict[str, Any] | None,
) -> bool:
    active_names = _active_application_names(context)
    if not active_names:
        return False
    raw = (context or {}).get("available_applications")
    if not isinstance(raw, list):
        return False
    active_keys = {_application_match_key(name) for name in active_names}
    text_key = _application_match_key(text)
    if not text_key:
        return False
    for item in raw:
        if not isinstance(item, dict):
            continue
        app_names = _application_identity_candidates(item)
        if not any(_application_match_key(name) in active_keys for name in app_names):
            continue
        for candidate in _application_match_candidates(item):
            candidate_key = _application_match_key(candidate)
            if len(candidate_key) >= 2 and candidate_key in text_key:
                return True
    return False


def _active_application_names(context: dict[str, Any] | None) -> list[str]:
    if not isinstance(context, dict):
        return []
    names: list[str] = []
    working_context = context.get("working_context")
    if isinstance(working_context, dict):
        names.extend(
            value.strip()
            for key in ("active_app", "launched_app", "app")
            for value in [working_context.get(key)]
            if isinstance(value, str) and value.strip()
        )
    latest_action_result = context.get("latest_action_result")
    if isinstance(latest_action_result, dict):
        names.extend(
            value.strip()
            for key in ("active_app", "launched_app", "app")
            for value in [latest_action_result.get(key)]
            if isinstance(value, str) and value.strip()
        )
    latest_observation = context.get("latest_observation")
    if isinstance(latest_observation, dict):
        names.extend(
            value.strip()
            for key in ("active_app", "launched_app", "app")
            for value in [latest_observation.get(key)]
            if isinstance(value, str) and value.strip()
        )
    return list(dict.fromkeys(names))


def _application_identity_candidates(item: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("name", "display_name", "bundle_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    aliases = item.get("aliases")
    if isinstance(aliases, list):
        candidates.extend(
            entry.strip()
            for entry in aliases
            if isinstance(entry, str) and entry.strip()
        )
    return list(dict.fromkeys(candidates))


def application_mentioned_in_text(
    app_name: str,
    text: str,
    context: dict[str, Any] | None,
) -> bool:
    text_key = _application_match_key(text)
    if not text_key:
        return False
    for candidate in _application_candidates_for_name(app_name, context):
        candidate_key = _application_match_key(candidate)
        if len(candidate_key) >= 2 and candidate_key in text_key:
            return True
    return False


def _application_candidates_for_name(
    app_name: str,
    context: dict[str, Any] | None,
) -> list[str]:
    candidates = [app_name]
    requested = _application_match_key(app_name)
    raw = (context or {}).get("available_applications")
    if not isinstance(raw, list) or not requested:
        return candidates
    for item in raw:
        if not isinstance(item, dict):
            continue
        identity_candidates = _application_identity_candidates(item)
        if not any(
            _application_match_key(candidate) == requested
            for candidate in identity_candidates
        ):
            continue
        candidates.extend(identity_candidates)
        candidates.extend(_application_match_candidates(item))
    return list(dict.fromkeys(candidates))


def _application_match_candidates(item: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("name", "display_name", "kind"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    for key in ("aliases", "capabilities", "categories", "keywords"):
        value = item.get(key)
        if isinstance(value, list):
            candidates.extend(
                entry.strip()
                for entry in value
                if isinstance(entry, str) and entry.strip()
            )
    return list(dict.fromkeys(candidates))


def _context_supports_action(
    context: dict[str, Any] | None,
    action_name: str,
) -> bool:
    capabilities = (context or {}).get("capabilities")
    if not capabilities:
        return True
    candidates = _action_capability_candidates(action_name)
    if isinstance(capabilities, dict):
        for candidate in candidates:
            value = capabilities.get(candidate)
            if _capability_value_enabled(value):
                return True
        return False
    if isinstance(capabilities, list):
        for item in capabilities:
            if isinstance(item, str) and item in candidates:
                return True
            if isinstance(item, dict):
                name = item.get("name") or item.get("capability") or item.get("id")
                if isinstance(name, str) and name in candidates:
                    return _capability_value_enabled(item)
        return False
    return True


def _action_capability_candidates(action_name: str) -> set[str]:
    namespace = action_name.split(".", 1)[0]
    legacy = {
        "browser": {"browser_control", "open_url"},
        "app": {"app_control"},
    }
    return {action_name, namespace, *legacy.get(namespace, set())}


def _capability_value_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return value.get("enabled", True) is not False
    return bool(value)


def _looks_like_browser_search_request(text: str) -> bool:
    lowered = text.casefold()
    compact = _application_match_key(text)
    search_terms = (
        "검색",
        "찾아줘",
        "찾아 줘",
        "찾아봐",
        "찾아 봐",
        "찾아줄래",
        "찾아 줄래",
        "search",
        "find",
        "look up",
        "lookup",
    )
    if any(term in lowered for term in search_terms):
        return True
    return any(term in compact for term in ("찾아줘", "찾아봐", "찾아줄래"))


def _looks_like_search_open_first_request(text: str) -> bool:
    lowered = text.casefold()
    compact = _application_match_key(text)
    open_terms = (
        "들어가",
        "들어가줘",
        "들어가 줘",
        "열어줘",
        "열어 줘",
        "open",
        "go to",
    )
    return any(term in lowered for term in open_terms) or any(
        term in compact for term in ("들어가", "들어가줘", "열어줘")
    )


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
