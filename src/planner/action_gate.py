from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionIntentGate:
    should_act: bool
    intent: str | None
    confidence: float
    reason: str | None
    template_key: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)


def parse_intent_gate(content: str) -> ActionIntentGate:
    data = _extract_json_object_or_intent_pairs(content)
    should_act = bool(data.get("should_act", False))
    slots = data.get("slots")
    slots = dict(slots) if isinstance(slots, dict) else {}
    raw_confidence = data.get("confidence")
    if raw_confidence is None and "confidence" in slots:
        raw_confidence = slots.get("confidence")
    confidence = _coerce_float(raw_confidence, default=0.0)
    confidence = max(0.0, min(confidence, 1.0))
    intent = data.get("intent")
    reason = data.get("reason")
    template_key = data.get("template_key")
    slots.pop("confidence", None)
    return ActionIntentGate(
        should_act=should_act,
        intent=(
            str(intent) if intent is not None else ("action" if should_act else "none")
        ),
        confidence=confidence,
        reason=str(reason) if reason is not None else None,
        template_key=str(template_key) if template_key is not None else None,
        slots=slots,
    )


def intent_gate_payload(gate: ActionIntentGate | None) -> dict[str, Any] | None:
    if gate is None:
        return None
    return {
        "should_act": gate.should_act,
        "intent": gate.intent,
        "confidence": gate.confidence,
        "reason": gate.reason,
        "template_key": gate.template_key,
        "slots": gate.slots,
    }


def intent_gate_prompt() -> str:
    return """You are JARVIS Action Intent Gate.
Output only valid JSON. Start with { and end with }.
Do not answer the user. Do not create actions.

Decide whether the user asks JARVIS to operate the local computer.
Use should_act=false for ordinary conversation or information-only questions.
Use should_act=false for recommendation, advice, explanation, summary,
brainstorming, or menu suggestion requests. Do not convert those into web search
unless the user explicitly asks to search/open a browser or operate the computer.
Use should_act=true for browser, app, shell, keyboard, mouse, file, clipboard,
or screen operation requests.
Use runtime_context.working_context only as short-term session state. It may
identify the active app, browser, and last typed text from immediately previous
actions.
The user may speak Korean. Treat polite or indirect Korean request endings as
commands when the verb asks JARVIS to operate the computer. Examples include
"~해줘", "~해봐", "~해볼래?", "~해줄래?", "~열어볼래?", "~작성해줘",
and "~작성해볼래?".
Requests that open/focus an app and then write/input/type text are actions,
including Korean forms using "작성", "입력", or "타이핑" after an app name.
But Korean answer requests such as "추천해줘", "설명해줘", "알려줘",
"요약해줘", "정리해줘", and "답해줘" are not actions unless paired with
explicit computer operation words like browser/search/open/click/type.
If runtime_context.working_context.active_app and last_typed_text are present,
short follow-up writing requests can be actions even when the app name is omitted.
Examples include asking to translate, rewrite, continue, shorten, or write the
previously typed content in another language. Fill slots.app_name from
working_context.active_app and slots.text with the concrete final text to type.
Do not mark greetings, thanks, casual chat, or factual questions as actions.
Screen capture requests such as "현재화면 캡쳐", "화면 캡처", "스크린샷 찍어줘",
or "현재화면 캡쳐해서 사진으로 띄워줘" are actions.

When should_act=true, choose the best template_key and fill slots from the
current user request. Supported template_key values:
- browser_open: open a browser with no query.
- browser_search: search the browser; slots.query is required.
- browser_search_open_first: search and open a result; slots.query is required
  and slots.open_result_index should be 1 unless the user specifies another
  visible result number.
- app_open: open a local application; slots.app_name is required.
- app_open_type: open a local application and type text; slots.app_name and
  slots.text are required.
- screen_screenshot: capture the current screen.

If should_act=false, use template_key=null and slots={}.
Never leave required slots empty for should_act=true.
Do not simplify multi-operation requests. If the user asks to open or focus an
app and then write, create, compose, input, or type content in that app, choose
app_open_type rather than app_open.
For app_open_type slots.text:
- If the user provides exact text, copy that text.
- If the user asks JARVIS to write/create/compose generated content, put the
  concrete final text that should be typed. Do not put placeholders or meta
  instructions such as "your introduction" in slots.text.

Example action output:
{"should_act":true,"intent":"browser.open","template_key":"browser_open",
"slots":{},"confidence":0.95,"reason":"operate browser"}

Example Korean polite action:
User: 브라우저 열어볼래?
{"should_act":true,"intent":"browser.open","template_key":"browser_open",
"slots":{},"confidence":0.95,"reason":"Korean action request"}

Example Korean search and enter action:
User: 네이버웹툰 검색해서 들어가 줘
{"should_act":true,"intent":"browser.search+browser.select_result",
"template_key":"browser_search_open_first",
"slots":{"query":"네이버웹툰","open_result_index":1},
"confidence":0.95,"reason":"search and open first result"}

Example Korean app input action:
User: sublimetext켜서 안녕하세요 작성해볼래?
{"should_act":true,"intent":"app.open+keyboard.type",
"template_key":"app_open_type",
"slots":{"app_name":"sublimetext","text":"안녕하세요"},
"confidence":0.95,"reason":"open app and type text"}

Example Korean app generated writing action:
User: 텍스트 편집기 켜서 너의 소개 작성해줘
{"should_act":true,"intent":"app.open+keyboard.type",
"template_key":"app_open_type",
"slots":{"app_name":"텍스트 편집기",
"text":"안녕하세요. 저는 JARVIS입니다. 컴퓨터 작업을 돕는 AI 어시스턴트입니다."},
"confidence":0.95,"reason":"open app, compose requested content, and type it"}

Example contextual follow-up action:
runtime_context.working_context.active_app: "Sublime Text"
runtime_context.working_context.last_typed_text: "안녕하세요. 저는 JARVIS입니다."
User: 영어로 작성해봐
{"should_act":true,"intent":"app.open+keyboard.type",
"template_key":"app_open_type",
"slots":{"app_name":"Sublime Text",
"text":"Hello. I am JARVIS."},
"confidence":0.9,"reason":"rewrite previous typed content in English in active app"}

Example Korean screenshot action:
User: 현재화면 캡쳐해서 사진으로 띄워줘
{"should_act":true,"intent":"screen.screenshot",
"template_key":"screen_screenshot","slots":{},
"confidence":0.95,"reason":"capture current screen"}

Example no-action output:
{"should_act":false,"intent":"none","template_key":null,"slots":{},
"confidence":0.95,"reason":"ordinary conversation"}

Example Korean recommendation no-action:
User: 점심 메뉴 추천해줘
{"should_act":false,"intent":"none","template_key":null,"slots":{},
"confidence":0.95,"reason":"recommendation answer request"}
"""


def _extract_json_object_or_intent_pairs(content: str) -> dict[str, Any]:
    try:
        return _extract_json_object(content)
    except json.JSONDecodeError:
        pairs = dict(
            (key.strip().lower(), value.strip().strip("\"'"))
            for key, value in re.findall(
                r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([^,\\n]+)",
                content,
            )
        )
        if not pairs:
            raise
        if "should_act" in pairs:
            pairs["should_act"] = pairs["should_act"].lower() == "true"
        if "confidence" in pairs:
            pairs["confidence"] = _coerce_float(pairs["confidence"], default=0.0)
        return pairs


def _extract_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")
    return parsed


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
