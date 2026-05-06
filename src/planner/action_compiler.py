from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from jarvis_contracts import (
    ClientAction,
    ClientActionPlan,
    ClientActionV2,
    ClientActionValidationIssue,
    format_action_v2_registry_for_prompt,
    normalize_action_payload,
)
from pydantic import ValidationError

from planner.action_adapter import V2ToV1ActionAdapter
from planner.action_validator import ActionValidator

logger = logging.getLogger("jarvis_controller.action_compiler")

DIRECT_EXECUTION_MODES = {"direct", "direct_sequence"}


@dataclass(frozen=True)
class ActionIntentDecision:
    should_act: bool
    execution_mode: str
    intent: str | None
    confidence: float
    reason: str | None
    actions: list[ClientAction]
    plan: ClientActionPlan | None = None
    validation_errors: list[ClientActionValidationIssue] | None = None


@dataclass(frozen=True)
class EmbeddedActionParseResult:
    saw_action_block: bool
    actions: list[ClientAction]
    issues: list[ClientActionValidationIssue]


class ActionCompiler:
    """Compile natural language into ActionContract v2 plans via the model."""

    def __init__(
        self,
        *,
        validator: ActionValidator | None = None,
        adapter: V2ToV1ActionAdapter | None = None,
    ) -> None:
        self.validator = validator or ActionValidator()
        self.adapter = adapter or V2ToV1ActionAdapter(validator=self.validator)

    def compile_plan(
        self,
        *,
        message: str,
        context: dict[str, Any] | None = None,
        latest_observation: dict[str, Any] | None = None,
        validation_errors: list[ClientActionValidationIssue] | None = None,
    ) -> ClientActionPlan | None:
        if not message.strip():
            return ClientActionPlan(mode="no_action", confidence=1.0, reason="empty message")
        if not _enabled():
            return None

        endpoint = os.getenv(
            "JARVIS_ACTION_INTENT_MODEL_ENDPOINT",
            "https://qwen.breakpack.cc/engines/v1/chat/completions",
        )
        model = os.getenv(
            "JARVIS_ACTION_INTENT_MODEL_NAME",
            "docker.io/ai/gemma3-qat:4B",
        )
        timeout = _float_env("JARVIS_ACTION_INTENT_MODEL_TIMEOUT_SECONDS", 2.5)
        max_tokens = int(_float_env("JARVIS_ACTION_INTENT_MODEL_MAX_TOKENS", 260))

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "runtime_context": context or {},
                            "latest_observation": latest_observation or {},
                            "validation_errors": [
                                issue.model_dump()
                                for issue in (validation_errors or [])
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens,
        }

        try:
            data = _post_json(endpoint, payload, timeout=timeout)
            content = str(
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            return _parse_plan(content)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            logger.warning(
                "action compiler failed endpoint=%s model=%s timeout=%.1fs error=%s",
                endpoint,
                model,
                timeout,
                exc,
            )
            return None
        except Exception as exc:
            logger.warning("action compiler failed: %s", exc)
            return None

    def compile_decision(
        self,
        *,
        message: str,
        context: dict[str, Any] | None = None,
        latest_observation: dict[str, Any] | None = None,
        validation_errors: list[ClientActionValidationIssue] | None = None,
        max_retries: int = 1,
    ) -> ActionIntentDecision | None:
        plan = self.compile_plan(
            message=message,
            context=context,
            latest_observation=latest_observation,
            validation_errors=validation_errors,
        )
        if plan is None:
            return None

        decision = self._decision_from_plan(plan, context=context)
        retries_left = max_retries
        while (
            decision is not None
            and decision.validation_errors
            and retries_left > 0
        ):
            retry_plan = self.compile_plan(
                message=message,
                context=context,
                latest_observation=latest_observation,
                validation_errors=decision.validation_errors,
            )
            if retry_plan is None:
                break
            decision = self._decision_from_plan(retry_plan, context=context)
            retries_left -= 1
        return decision

    def _decision_from_plan(
        self,
        plan: ClientActionPlan,
        *,
        context: dict[str, Any] | None,
    ) -> ActionIntentDecision:
        if plan.mode == "no_action":
            return ActionIntentDecision(
                should_act=False,
                execution_mode="no_action",
                intent="none",
                confidence=plan.confidence,
                reason=plan.reason,
                actions=[],
                plan=plan,
                validation_errors=[],
            )

        adapted = self.adapter.adapt_plan(plan, context=context)
        if adapted.valid:
            execution_mode = plan.mode
            if execution_mode not in {"direct", "direct_sequence", "needs_plan"}:
                execution_mode = "direct_sequence" if len(adapted.actions) > 1 else "direct"
            return ActionIntentDecision(
                should_act=bool(adapted.actions),
                execution_mode=execution_mode,
                intent=_intent_from_plan(plan),
                confidence=plan.confidence,
                reason=plan.reason,
                actions=adapted.actions,
                plan=plan,
                validation_errors=[],
            )

        return ActionIntentDecision(
            should_act=False,
            execution_mode="invalid",
            intent=_intent_from_plan(plan),
            confidence=plan.confidence,
            reason=plan.reason,
            actions=[],
            plan=plan,
            validation_errors=adapted.issues,
        )


def classify_client_action_intent_decision(
    message: str,
    *,
    context: dict[str, Any] | None = None,
    latest_observation: dict[str, Any] | None = None,
    validation_errors: list[ClientActionValidationIssue] | None = None,
) -> ActionIntentDecision | None:
    return ActionCompiler().compile_decision(
        message=message,
        context=context,
        latest_observation=latest_observation,
        validation_errors=validation_errors,
    )


def compile_action_decision_from_model_text(
    content: str,
    *,
    context: dict[str, Any] | None = None,
) -> ActionIntentDecision | None:
    try:
        plan = _parse_plan(content)
    except Exception as exc:
        logger.warning("action compiler fallback response parse failed: %s", exc)
        return None
    return ActionCompiler()._decision_from_plan(plan, context=context)


def action_compiler_prompt_payload(
    *,
    message: str,
    context: dict[str, Any] | None = None,
    validation_errors: list[ClientActionValidationIssue] | None = None,
) -> str:
    return (
        _system_prompt()
        + "\n\nInput JSON:\n"
        + json.dumps(
            {
                "message": message,
                "runtime_context": context or {},
                "validation_errors": [
                    issue.model_dump()
                    for issue in (validation_errors or [])
                ],
            },
            ensure_ascii=False,
        )
    )


def should_try_client_action_classifier(message: str) -> bool:
    """Only skip empty messages. No keyword or phrase gate is allowed."""
    return bool(message.strip())


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


def _parse_plan(content: str) -> ClientActionPlan:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|actions)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.lstrip().startswith("["):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    parsed = json.loads(text)
    if isinstance(parsed, list):
        return ClientActionPlan(
            mode="direct_sequence" if len(parsed) > 1 else "direct",
            confidence=0.8,
            reason="action array response",
            actions=[ClientActionV2.model_validate(item) for item in parsed],
        )
    if not isinstance(parsed, dict):
        raise ValueError("compiler response is not a JSON object")
    if "should_act" in parsed and "mode" not in parsed:
        parsed = _legacy_plan_payload(parsed)
    return ClientActionPlan.model_validate(parsed)


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
        return payload

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
                        "search_engine": args.get("search_engine") or args.get("engine"),
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
                        "search_engine": args.get("search_engine") or args.get("engine"),
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
                "index": _coerce_result_index(
                    args.get("index") or target or payload_text
                )
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
                        "search_engine": args.get("search_engine") or args.get("engine"),
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


def _system_prompt() -> str:
    registry = format_action_v2_registry_for_prompt()
    return f"""You are the JARVIS Action Compiler.
Return exactly one JSON object. No markdown. No prose.

Compile the user message into an ActionContract v2 ClientActionPlan.
Do not answer the user. Do not emit v1 action types.
If the request is ordinary conversation or information-only, return mode "no_action".
If validation_errors are provided, fix the structured plan according to those errors.

Rules:
- Use only capability names from the registry.
- Use app.open/app.focus only for concrete local applications.
- Never use target "browser", "default_browser", or "web_browser" for app actions.
- Use browser.open/browser.navigate/browser.search for browser work.
- Use browser.extract_dom before browser.click/browser.type when the element id is unknown.
- Use browser.select_result only when the user asks to open a numbered result
  already visible in current search results.
- terminal.run and calendar create/update/delete require requires_confirm=true.
- Disabled or unavailable capabilities in runtime_context must not be used.
- Do not invent missing URLs. If only a query is known, use browser.search with args.query.

Capability registry:
{registry}

JSON schema:
{{
  "mode": "direct|direct_sequence|needs_plan|no_action",
  "goal": "short goal or null",
  "confidence": 0.0,
  "reason": "short reason",
  "actions": [
    {{
      "name": "browser.search",
      "target": null,
      "payload": null,
      "args": {{"query": "search terms"}},
      "description": "human readable action",
      "requires_confirm": false
    }}
  ]
}}
"""


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


def _without_none_v2(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "JARVIS/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"HTTP {exc.code} from action compiler: {error_body[:500]}"
        ) from exc


def _intent_from_plan(plan: ClientActionPlan) -> str | None:
    if not plan.actions:
        return "none" if plan.mode == "no_action" else None
    namespaces = {action.name.split(".", 1)[0] for action in plan.actions}
    return next(iter(namespaces)) if len(namespaces) == 1 else "multi_action"


def _enabled() -> bool:
    raw = os.getenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


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
