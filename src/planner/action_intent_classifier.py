from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

from jarvis_contracts import ClientAction

logger = logging.getLogger("jarvis_controller.action_intent_classifier")

ALLOWED_ACTION_TYPES = {
    "app_control",
    "open_url",
    "browser_control",
    "keyboard_type",
    "hotkey",
    "clipboard",
    "notify",
}

ALLOWED_BROWSER_COMMANDS = {
    "extract_dom",
    "select_result",
    "scroll",
    "back",
    "forward",
    "reload",
}


def classify_client_action_intent(
    message: str,
    *,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    if not _enabled() or not _should_try_classifier(message, context):
        return []

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
            return []
        action_payloads = parsed.get("actions")
        if isinstance(action_payloads, list):
            actions = [
                action
                for item in action_payloads
                if isinstance(item, dict)
                for action in [_coerce_client_action(item)]
                if action is not None
            ]
            return actions
        action_payload = parsed.get("action")
        if isinstance(action_payload, dict):
            action = _coerce_client_action(action_payload)
            return [action] if action is not None else []
        return []
    except Exception as exc:
        logger.warning("action intent classifier failed: %s", exc)
        return []


def _enabled() -> bool:
    raw = os.getenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def _should_try_classifier(message: str, context: dict[str, Any] | None) -> bool:
    text = message.lower()
    if context and context.get("browser_active"):
        return _has_any(
            text,
            "열어",
            "켜",
            "켜서",
            "앱",
            "작성",
            "입력",
            "써",
            "타이핑",
            "들어가",
            "클릭",
            "선택",
            "open",
            "click",
            "select",
        )
    return _has_any(
        text,
        "브라우저",
        "크롬",
        "앱",
        "어플",
        "페이지",
        "검색 결과",
        "열어",
        "켜",
        "켜서",
        "작성",
        "입력",
        "써",
        "타이핑",
        "들어가",
        "클릭",
        "선택",
        "스크롤",
        "뒤로",
        "앞으로",
        "새로고침",
        "browser",
        "chrome",
        "page",
        "open",
        "click",
        "select",
        "scroll",
        "back",
        "forward",
        "reload",
    )


def _system_prompt() -> str:
    return """You are a strict action intent classifier for JARVIS.
Return only one JSON object. No markdown. No prose.

Decide whether the user wants the client runtime to perform an action.
Do not answer the user. Do not invent URLs.

Context fields may include:
- browser_active: true when a browser/search page was recently opened
- last_query: previous browser search query
- last_url: previous opened URL

Action policy:
- If the user asks to open/click/select something on the current page or previous search results, emit:
  type=browser_control, command=extract_dom, target=active_tab,
  args={purpose:"resolve_open_request", query:"the item to open", include_links:true, max_links:120}
- If the user asks to open a URL, emit type=open_url with target URL.
- If the user asks to search in a browser, emit type=open_url with Google search URL and args.query.
- If the user asks scroll/back/forward/reload, emit browser_control with that command.
- If the user asks to open/launch an app, emit type=app_control, command=open, target=app name.
- If the user asks to open an app and write/type text, emit two actions in order:
  1. app_control open target=app name
  2. keyboard_type payload=the exact text to type, args={enter:false}
- If it is ordinary chat or an informational question, should_act=false.

JSON schema:
{
  "should_act": boolean,
  "intent": "none|browser_search|open_url|open_link_from_current_page|browser_control|app_control|app_open_and_type",
  "confidence": number,
  "reason": "short reason",
  "actions": [
    {
      "type": "open_url|browser_control|app_control|keyboard_type|hotkey|clipboard|notify",
      "command": string|null,
      "target": string|null,
      "payload": string|null,
      "args": object,
      "description": string,
      "requires_confirm": boolean
    }
  ] | null,
  "action": {
    "type": "open_url|browser_control|app_control|keyboard_type|hotkey|clipboard|notify",
    "command": string|null,
    "target": string|null,
    "payload": string|null,
    "args": object,
    "description": string,
    "requires_confirm": boolean
  } | null
}
"""


def _post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


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
    action_type = payload.get("type")
    command = payload.get("command")
    if action_type not in ALLOWED_ACTION_TYPES:
        return None
    if action_type == "browser_control" and command not in ALLOWED_BROWSER_COMMANDS:
        return None
    return ClientAction(
        type=action_type,
        command=command if isinstance(command, str) else None,
        target=payload.get("target") if isinstance(payload.get("target"), str) else None,
        payload=payload.get("payload") if isinstance(payload.get("payload"), str) else None,
        args=payload.get("args") if isinstance(payload.get("args"), dict) else {},
        description=(
            payload.get("description")
            if isinstance(payload.get("description"), str)
            else "클라이언트 액션 실행"
        ),
        requires_confirm=bool(payload.get("requires_confirm", False)),
    )


def _has_any(text: str, *tokens: str) -> bool:
    return any(token in text for token in tokens)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
