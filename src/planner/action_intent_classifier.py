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
    ACTION_INTENT_ACTION_TYPES,
    COMMANDS_BY_ACTION_TYPE,
    ClientAction,
    format_action_registry_for_prompt,
    normalize_action_payload,
)

logger = logging.getLogger("jarvis_controller.action_intent_classifier")

ALLOWED_ACTION_TYPES = set(ACTION_INTENT_ACTION_TYPES)
ALLOWED_BROWSER_COMMANDS = set(COMMANDS_BY_ACTION_TYPE["browser_control"])
ALLOWED_CALENDAR_COMMANDS = set(COMMANDS_BY_ACTION_TYPE["calendar_control"])
ALLOWED_TERMINAL_COMMANDS = set(COMMANDS_BY_ACTION_TYPE["terminal"])

DIRECT_EXECUTION_MODES = {"direct", "direct_sequence"}
ALLOWED_EXECUTION_MODES = {*DIRECT_EXECUTION_MODES, "needs_plan", "no_action"}


@dataclass(frozen=True)
class ActionIntentDecision:
    should_act: bool
    execution_mode: str
    intent: str | None
    confidence: float
    reason: str | None
    actions: list[ClientAction]


def classify_client_action_intent_decision(
    message: str,
    *,
    context: dict[str, Any] | None = None,
) -> ActionIntentDecision | None:
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
    timeout = _float_env("JARVIS_ACTION_INTENT_MODEL_TIMEOUT_SECONDS", 4.0)
    threshold = _float_env("JARVIS_ACTION_INTENT_CONFIDENCE_THRESHOLD", 0.72)
    max_tokens = int(_float_env("JARVIS_ACTION_INTENT_MODEL_MAX_TOKENS", 180))

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message": message,
                        "context": context or {},
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
        parsed = _parse_json_object(content)
        confidence = float(parsed.get("confidence") or 0)
        if not parsed.get("should_act") or confidence < threshold:
            intent = _string_or_none(parsed.get("intent"))
            logger.info(
                "action intent classifier selected mode=%s actions=0 intent=%s confidence=%.2f message=%s",
                "no_action",
                intent,
                confidence,
                message[:160],
            )
            return ActionIntentDecision(
                should_act=False,
                execution_mode="no_action",
                intent=intent,
                confidence=confidence,
                reason=_string_or_none(parsed.get("reason")),
                actions=[],
            )
        action_payloads = parsed.get("actions")
        actions: list[ClientAction] = []
        if isinstance(action_payloads, list):
            actions = [
                action
                for item in action_payloads
                if isinstance(item, dict)
                for action in [_coerce_client_action(item)]
                if action is not None
            ]
        elif isinstance(parsed.get("action"), dict):
            action_payload = parsed["action"]
            action = _coerce_client_action(action_payload)
            actions = [action] if action is not None else []
        intent = _string_or_none(parsed.get("intent"))
        execution_mode = _execution_mode(parsed.get("execution_mode"), actions)
        logger.info(
            "action intent classifier selected mode=%s actions=%d intent=%s confidence=%.2f message=%s",
            execution_mode,
            len(actions),
            intent,
            confidence,
            message[:160],
        )
        return ActionIntentDecision(
            should_act=bool(parsed.get("should_act")),
            execution_mode=execution_mode,
            intent=intent,
            confidence=confidence,
            reason=_string_or_none(parsed.get("reason")),
            actions=actions,
        )
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        logger.warning(
            "action intent classifier failed endpoint=%s model=%s timeout=%.1fs error=%s",
            endpoint,
            model,
            timeout,
            exc,
        )
        return None
    except Exception as exc:
        logger.warning("action intent classifier failed: %s", exc)
        return None


def _enabled() -> bool:
    raw = os.getenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def _system_prompt() -> str:
    allowed_types = "|".join(ACTION_INTENT_ACTION_TYPES)
    registry = format_action_registry_for_prompt(direct_only=True)
    template = """You classify whether JARVIS must execute a client action.
Return only one JSON object. No markdown. No prose.

Infer meaning from the user message and context. Do not answer the user. Do not invent URLs.

Context:
- platform: "macos", "windows", "linux", or "unknown".
- shell: preferred shell such as "zsh", "bash", "powershell", or "cmd".
- default_browser: preferred browser such as "chrome", "safari", or "edge".
- capabilities: supported client action types/commands.
- calendar_provider: user-configured calendar provider/app, or "none".
- timezone: user's local timezone.
- browser_active means a browser/search page was recently opened; last_query/last_url describe it.

Execution mode:
- direct: one simple client action. Never plan.
- direct_sequence: a short ordered sequence of client actions. Never plan.
- needs_plan: complex work requiring investigation, planning, multiple reasoning steps, or content synthesis after actions.
- no_action: ordinary chat or informational answer. No client action.

Action policy:
- Current page click/open/select: browser_control extract_dom target=active_tab args={purpose:"resolve_open_request",query:"item",include_links:true,max_links:120}
- Current page typing: browser_control extract_dom target=active_tab args={purpose:"resolve_type_request",query:"field",text:"exact text",include_elements:true,max_links:120}
- Open URL: open_url target=url.
- Browser search: open_url target=https://www.google.com/search?q=... args={query:"search terms",browser:"chrome"}.
- Scroll/back/forward/reload: browser_control command=scroll/back/forward/reload.
- Open app: app_control command=open target=app name.
- Open app and type: actions=[app_control open, keyboard_type payload=exact text args={enter:false}].
- Calendar: use calendar_control. commands=open/list_events/create_event/update_event/delete_event. Use user's calendar_provider and timezone from context. create/update/delete require requires_confirm=true.
- Terminal commands: use terminal command=execute. Use target/context shell ("powershell" on Windows, "zsh" or "bash" on macOS/Linux) and payload as the command string. Terminal actions require requires_confirm=true.
- For simple browser/app control, use direct/direct_sequence even if the user used natural language.
- Set requires_confirm=false for ordinary open/search/scroll/type actions; true only for destructive or sensitive actions.
- If it is ordinary chat or an informational question, should_act=false.

Canonical action registry:
__ACTION_REGISTRY__

JSON schema:
{
  "should_act": boolean,
  "execution_mode": "direct|direct_sequence|needs_plan|no_action",
  "intent": "none|browser_search|open_url|open_link_from_current_page|browser_control|app_control|app_open_and_type|calendar_control|terminal",
  "confidence": number,
  "reason": "short reason",
  "actions": [
    {
      "type": "__ALLOWED_TYPES__",
      "command": string|null,
      "target": string|null,
      "payload": string|null,
      "args": object,
      "description": string,
      "requires_confirm": boolean
    }
  ] | null,
  "action": {
    "type": "__ALLOWED_TYPES__",
    "command": string|null,
    "target": string|null,
    "payload": string|null,
    "args": object,
    "description": string,
    "requires_confirm": boolean
  } | null
}
"""
    return template.replace("__ACTION_REGISTRY__", registry).replace(
        "__ALLOWED_TYPES__", allowed_types
    )


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
            f"HTTP {exc.code} from action intent model: {error_body[:500]}"
        ) from exc


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("classifier response is not a JSON object")
    return parsed


def _coerce_client_action(payload: dict[str, Any]) -> ClientAction | None:
    payload = normalize_action_payload(payload)
    action_type = payload.get("type")
    command = payload.get("command")
    if action_type not in ALLOWED_ACTION_TYPES:
        return None
    if action_type == "app_control" and command not in COMMANDS_BY_ACTION_TYPE["app_control"]:
        return None
    if action_type == "browser_control" and command not in ALLOWED_BROWSER_COMMANDS:
        return None
    if action_type == "calendar_control" and command not in ALLOWED_CALENDAR_COMMANDS:
        return None
    if action_type == "terminal" and command not in ALLOWED_TERMINAL_COMMANDS:
        return None
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    if action_type == "browser_control" and command in {"click_element", "type_element"}:
        raw_ai_id = args.get("ai_id")
        if not isinstance(raw_ai_id, int):
            return None
    return ClientAction(
        type=action_type,
        command=command if isinstance(command, str) else None,
        target=payload.get("target") if isinstance(payload.get("target"), str) else None,
        payload=payload.get("payload") if isinstance(payload.get("payload"), str) else None,
        args=args,
        description=(
            payload.get("description")
            if isinstance(payload.get("description"), str)
            else "클라이언트 액션 실행"
        ),
        requires_confirm=bool(payload.get("requires_confirm", False)),
    )


def _execution_mode(raw: Any, actions: list[ClientAction]) -> str:
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in ALLOWED_EXECUTION_MODES:
            return normalized
    if len(actions) > 1:
        return "direct_sequence"
    if actions:
        return "direct"
    return "no_action"


def _string_or_none(raw: Any) -> str | None:
    return raw if isinstance(raw, str) else None


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
