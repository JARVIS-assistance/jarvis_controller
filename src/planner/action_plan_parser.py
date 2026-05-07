from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jarvis_contracts import (
    ClientAction,
    ClientActionPlan,
    ClientActionV2,
    ClientActionValidationIssue,
    normalize_action_payload,
)
from pydantic import ValidationError

from planner.action_adapter import V2ToV1ActionAdapter
from planner.action_validator import ActionValidator

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass(frozen=True)
class EmbeddedActionParseResult:
    saw_action_block: bool
    actions: list[ClientAction]
    issues: list[ClientActionValidationIssue]


def parse_embedded_actions_from_text(
    content: str,
    *,
    context: dict[str, Any] | None = None,
) -> EmbeddedActionParseResult:
    saw_action_block = False
    actions: list[ClientAction] = []
    issues: list[ClientActionValidationIssue] = []
    validator = ActionValidator()
    adapter = V2ToV1ActionAdapter(validator=validator)

    for raw in _action_block_payloads(content):
        saw_action_block = True
        items = raw if isinstance(raw, list) else [raw]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                issues.append(
                    _issue(
                        "invalid_embedded_action",
                        "Embedded action item is not an object",
                        action_index=index,
                    )
                )
                continue
            if "name" in item or "actions" in item or "mode" in item:
                plan = _embedded_v2_plan(item)
                if plan is None:
                    issues.append(
                        _issue(
                            "invalid_v2_action_block",
                            "Embedded v2 action block is not a valid ClientActionPlan",
                            action_index=index,
                        )
                    )
                    continue
                adapted = adapter.adapt_plan(plan, context=context)
                if adapted.valid:
                    actions.extend(adapted.actions)
                else:
                    issues.extend(adapted.issues)
                continue

            recovered_plan = _embedded_legacy_v1_plan(item)
            if recovered_plan is not None:
                adapted = adapter.adapt_plan(recovered_plan, context=context)
                if adapted.valid:
                    actions.extend(adapted.actions)
                    continue
                issues.extend(adapted.issues)
                continue

            try:
                action = ClientAction.model_validate(normalize_action_payload(item))
            except ValidationError as exc:
                issues.append(
                    _issue(
                        "invalid_v1_action_block",
                        "Embedded v1 action block is not a valid ClientAction",
                        action_index=index,
                        details={"errors": exc.errors()},
                    )
                )
                continue
            validation = validator.validate_v1_actions([action], context=context)
            if validation.valid:
                actions.extend(validation.actions)
            else:
                issues.extend(validation.issues)

    return EmbeddedActionParseResult(saw_action_block, actions, issues)


def coerce_client_actions_from_text(
    content: str,
    *,
    context: dict[str, Any] | None = None,
    message: str | None = None,
) -> list[ClientAction]:
    _ = message
    return parse_embedded_actions_from_text(content, context=context).actions


def parse_plan(content: str) -> ClientActionPlan:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|actions)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = _loads_model_structured_data(text)
    except Exception:
        if text.lstrip().startswith("["):
            raise
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = _loads_model_structured_data(text[start : end + 1])
    if isinstance(parsed, list):
        return ClientActionPlan(
            mode="direct_sequence" if len(parsed) > 1 else "direct",
            confidence=0.8,
            reason="action array response",
            actions=[
                ClientActionV2.model_validate(_normalize_v2_action_payload(item))
                for item in parsed
                if isinstance(item, dict)
            ],
        )
    if not isinstance(parsed, dict):
        raise ValueError("compiler response is not a JSON object")
    if "should_act" in parsed and "mode" not in parsed:
        parsed = _legacy_plan_payload(parsed)
    parsed = _normalize_plan_payload(parsed)
    return ClientActionPlan.model_validate(parsed)


def _embedded_v2_plan(item: dict[str, Any]) -> ClientActionPlan | None:
    try:
        if "actions" in item or "mode" in item:
            return ClientActionPlan.model_validate(item)
        return ClientActionPlan(
            mode="direct",
            confidence=0.8,
            reason="embedded v2 action",
            actions=[ClientActionV2.model_validate(item)],
        )
    except ValidationError:
        return None


def _embedded_legacy_v1_plan(item: dict[str, Any]) -> ClientActionPlan | None:
    try:
        return ClientActionPlan(
            mode="direct",
            confidence=0.75,
            reason="embedded legacy v1 action",
            actions=[ClientActionV2.model_validate(_legacy_action_payload_to_v2(item))],
        )
    except ValidationError:
        return None


def _action_block_payloads(content: str) -> list[Any]:
    payloads: list[Any] = []
    pattern = r"```(?:actions|json)\s*\n(.*?)```"
    for match in re.findall(pattern, content, re.DOTALL | re.IGNORECASE):
        try:
            payloads.append(json.loads(match.strip()))
        except json.JSONDecodeError:
            continue
    return payloads


def _loads_model_structured_data(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if yaml is not None:
            loaded = yaml.safe_load(text)
            if loaded is None:
                raise
            return loaded
        loaded = _loads_minimal_yaml_plan(text)
        if loaded is None:
            raise
        return loaded


def _loads_minimal_yaml_plan(text: str) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    actions: list[dict[str, Any]] = []
    current_action: dict[str, Any] | None = None
    in_actions = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            current_action = None
            if stripped == "actions:":
                result["actions"] = actions
                in_actions = True
                continue
            if ":" not in stripped:
                return None
            key, value = stripped.split(":", 1)
            result[key.strip()] = _minimal_yaml_scalar(value.strip())
            in_actions = False
            continue
        if not in_actions:
            continue
        if stripped.startswith("- "):
            current_action = {}
            actions.append(current_action)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if current_action is None or ":" not in stripped:
            return None
        key, value = stripped.split(":", 1)
        current_action[key.strip()] = _minimal_yaml_scalar(value.strip())

    if "mode" not in result and "actions" not in result:
        return None
    return result


def _minimal_yaml_scalar(value: str) -> Any:
    if value == "":
        return None
    lowered = value.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value == "{}":
        return {}
    if value == "[]":
        return []
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _legacy_plan_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    if not parsed.get("should_act"):
        return {
            "mode": "no_action",
            "goal": None,
            "actions": [],
            "confidence": float(parsed.get("confidence") or 0),
            "reason": parsed.get("reason"),
        }
    return {
        "mode": parsed.get("execution_mode") or "direct",
        "goal": parsed.get("intent"),
        "actions": [
            _legacy_action_payload_to_v2(item)
            for item in (
                parsed.get("actions")
                or ([] if parsed.get("action") is None else [parsed["action"]])
            )
            if isinstance(item, dict)
        ],
        "confidence": float(parsed.get("confidence") or 0),
        "reason": parsed.get("reason"),
    }


def _legacy_action_payload_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    if "name" in payload:
        return _normalize_v2_action_payload(payload)

    data = normalize_action_payload(payload)
    action_type = data.get("type")
    command = data.get("command")
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    target = data.get("target") if isinstance(data.get("target"), str) else None
    payload_text = data.get("payload") if isinstance(data.get("payload"), str) else None
    description = (
        data.get("description")
        if isinstance(data.get("description"), str)
        else "Client action"
    )
    requires_confirm = bool(data.get("requires_confirm", False))

    if action_type == "open_url":
        query = args.get("query")
        if isinstance(query, str) and query.strip():
            return {
                "name": "browser.search",
                "args": {
                    key: value
                    for key, value in {
                        "query": query.strip(),
                        "browser": args.get("browser"),
                        "search_engine": args.get("search_engine")
                        or args.get("engine"),
                    }.items()
                    if value is not None
                },
                "description": description,
                "requires_confirm": requires_confirm,
            }
        return {
            "name": "browser.navigate",
            "args": {
                key: value
                for key, value in {
                    "url": target or payload_text,
                    "browser": args.get("browser"),
                }.items()
                if value is not None
            },
            "description": description,
            "requires_confirm": requires_confirm,
        }

    if action_type == "browser_control":
        if command == "search":
            return {
                "name": "browser.search",
                "args": {
                    key: value
                    for key, value in {
                        "query": args.get("query") or payload_text,
                        "browser": args.get("browser"),
                        "search_engine": args.get("search_engine")
                        or args.get("engine"),
                    }.items()
                    if value is not None
                },
                "description": description,
                "requires_confirm": requires_confirm,
            }
        if command in {"open", "open_url", "navigate"}:
            return {
                "name": "browser.navigate",
                "args": {
                    key: value
                    for key, value in {
                        "url": target or payload_text,
                        "browser": args.get("browser"),
                    }.items()
                    if value is not None
                },
                "description": description,
                "requires_confirm": requires_confirm,
            }
        if command == "extract_dom":
            return {
                "name": "browser.extract_dom",
                "target": target,
                "args": args,
                "description": description,
                "requires_confirm": requires_confirm,
            }
        if command == "click_element":
            return {
                "name": "browser.click",
                "target": target,
                "args": {"ai_id": args.get("ai_id")},
                "description": description,
                "requires_confirm": requires_confirm,
            }
        if command == "type_element":
            return {
                "name": "browser.type",
                "target": target,
                "payload": payload_text,
                "args": {
                    "ai_id": args.get("ai_id"),
                    "text": args.get("text") or payload_text,
                    "enter": bool(args.get("enter", False)),
                },
                "description": description,
                "requires_confirm": requires_confirm,
            }
        if command == "select_result":
            return {
                "name": "browser.select_result",
                "target": target,
                "args": {
                    "index": _coerce_result_index(
                        args.get("index") or target or payload_text
                    )
                },
                "description": description,
                "requires_confirm": requires_confirm,
            }

    if action_type == "web_search":
        query = args.get("query") or args.get("search_query") or target or payload_text
        return {
            "name": "browser.search",
            "args": {
                key: value
                for key, value in {
                    "query": query,
                    "browser": args.get("browser"),
                    "search_engine": args.get("search_engine") or args.get("engine"),
                }.items()
                if value is not None
            },
            "description": description,
            "requires_confirm": requires_confirm,
        }

    if action_type in {"web_click", "web_select", "select_result"}:
        return {
            "name": "browser.select_result",
            "target": "active_tab",
            "args": {
                "index": _coerce_result_index(args.get("index") or target or payload_text)
            },
            "description": description,
            "requires_confirm": requires_confirm,
        }

    if action_type == "app_control":
        query = args.get("query") or args.get("search_query")
        target_value = (target or "").strip().lower()
        if (
            target_value in {"browser", "default_browser", "web_browser"}
            and isinstance(query, str)
            and query.strip()
        ):
            return {
                "name": "browser.search",
                "args": {
                    key: value
                    for key, value in {
                        "query": query.strip(),
                        "browser": args.get("browser"),
                        "search_engine": args.get("search_engine")
                        or args.get("engine"),
                    }.items()
                    if value is not None
                },
                "description": description,
                "requires_confirm": requires_confirm,
            }
        return {
            "name": "app.focus" if command == "focus" else "app.open",
            "target": target,
            "args": args,
            "description": description,
            "requires_confirm": requires_confirm,
        }

    if action_type == "keyboard_type":
        return {
            "name": "keyboard.type",
            "target": target,
            "payload": payload_text,
            "args": {"text": payload_text, "enter": bool(args.get("enter", False))},
            "description": description,
            "requires_confirm": requires_confirm,
        }

    if action_type == "hotkey":
        return {
            "name": "keyboard.hotkey",
            "target": target,
            "args": {"keys": args.get("keys")},
            "description": description,
            "requires_confirm": requires_confirm,
        }

    return payload


def _normalize_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return payload
    normalized = dict(payload)
    normalized["actions"] = [
        _normalize_v2_action_payload(action)
        for action in actions
        if isinstance(action, dict)
    ]
    return normalized


def _normalize_v2_action_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    args = normalized.get("args")
    normalized_args = dict(args) if isinstance(args, dict) else {}
    if "name" not in normalized and isinstance(normalized.get("action"), str):
        normalized["name"] = normalized["action"]
    action_name = normalized.get("name")
    if (
        action_name in {"app.open", "app.focus"}
        and not normalized.get("target")
        and isinstance(normalized_args.get("app_name"), str)
    ):
        normalized["target"] = normalized_args["app_name"]
    raw_payload = normalized.get("payload")
    if raw_payload is None or isinstance(raw_payload, str):
        normalized["args"] = normalized_args
        return normalized

    if isinstance(raw_payload, dict):
        for key, value in raw_payload.items():
            normalized_args.setdefault(str(key), value)
        normalized["payload"] = _first_string_arg(
            normalized_args,
            "payload",
            "text",
            "value",
            "command",
            "url",
            "query",
        )
    else:
        normalized["payload"] = str(raw_payload)
    normalized["args"] = normalized_args
    return normalized


def _first_string_arg(args: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


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

