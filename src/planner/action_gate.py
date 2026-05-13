from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - optional config dependency
    yaml = None

logger = logging.getLogger("jarvis_controller.action_gate")


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


_PROMPTS_YAML_ENV = "JARVIS_PROMPTS_YAML"
_ACTION_INTENT_GATE_PROMPT_KEY = "action_intent_gate"


def _default_prompts_yaml_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "jarvis_ai_workbench"
        / "config"
        / "prompts.yaml"
    )


def _prompts_yaml_path() -> Path:
    env_path = os.getenv(_PROMPTS_YAML_ENV)
    if env_path:
        return Path(env_path)
    return _default_prompts_yaml_path()


def _load_prompt_from_yaml(key: str) -> str | None:
    if yaml is None:
        return None
    path = _prompts_yaml_path()
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("failed to load prompt yaml path=%s error=%s", path, exc)
        return None

    prompt = data.get("prompts", {}).get(key)
    if not isinstance(prompt, dict):
        return None
    content = prompt.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


_INTENT_GATE_PROMPT_FALLBACK = """You are JARVIS Action Intent Gate.
Output only valid JSON. Start with { and end with }.
Do not answer the user. Do not create actions.

Decide whether the user asks JARVIS to operate the local computer.
Use should_act=false for ordinary conversation or information-only questions.
Use should_act=false for recommendation, advice, explanation, summary,
brainstorming, or menu suggestion requests. Do not convert those into web search
unless the user explicitly asks to search/open a browser or operate the computer.
Cuisine/category follow-ups after a recommendation question, such as "한식 레츠고",
are still answer-generation requests unless paired with search/browser/open words.
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
- browser_select_result: open an already visible browser search result;
  slots.index is required. Use this for follow-up requests such as opening the
  second/third result after a previous browser search.
- open_url or browser_navigate: open a concrete URL; slots.url is required.
- browser_extract_dom: inspect the active browser page when an element id is
  needed before click/type.
- browser_click: click a known browser DOM element; slots.ai_id is required.
- browser_type: type into a known browser DOM element; slots.ai_id and
  slots.text are required.
- app_open: open a local application; slots.app_name is required.
- app_open_type: open a local application and type text; slots.app_name and
  slots.text are required.
- app_focus or app_close: focus or close a local app; slots.app_name is required.
- file_read: read a file; slots.path is required.
- file_write: write a file; slots.path and slots.text are required and
  confirmation is required.
- terminal_run: run a terminal command; slots.command is required and
  confirmation is required.
- screen_screenshot: capture the current screen.
- mouse_click or mouse_drag: use screen coordinates; confirmation is required.
- keyboard_type or keyboard_hotkey: type text or press a shortcut.
- clipboard_copy or clipboard_paste: copy text or paste; paste requires
  confirmation.
- notification_show: show a notification; slots.text is required.
- web_search: run server-side web search without opening the browser;
  slots.query is required.

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

Example Korean browser page action:
User: 브라우저 열어서 네이버 웹툰 페이지 열어줘
{"should_act":true,"intent":"browser.search+browser.select_result",
"template_key":"browser_search_open_first",
"slots":{"query":"네이버 웹툰","open_result_index":1},
"confidence":0.95,"reason":"open requested browser page"}

Example browser result follow-up action:
runtime_context.working_context.active_surface: "browser"
runtime_context.working_context.active_browser: "https://www.google.com/search?q=소불고기+레시피"
User: 두번째 레시피 열어줘
{"should_act":true,"intent":"browser.select_result",
"template_key":"browser_select_result",
"slots":{"index":2},
"confidence":0.95,"reason":"open visible browser result"}

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

Example Korean recommendation follow-up no-action:
User: 한식 레츠고
{"should_act":false,"intent":"none","template_key":null,"slots":{},
"confidence":0.95,"reason":"menu recommendation follow-up"}
"""


_INTENT_GATE_POLICY_OVERLAY = """Fresh-context app priority:
- If runtime_context has no working_context and available application names or
  available_applications contain an app that matches the user's requested task
  by app name, alias, or clear task/domain fit, prefer template_key=app_open
  over browser_search. Use the exact available app name in slots.app_name.
- Treat current/local/live information requests as app-open candidates when an
  installed app advertises a matching capability, category, keyword, or alias.
  The frontend supplies those app facts; infer from runtime_context rather than
  from a hardcoded intent table.
- Do not invent unavailable apps. If no available app matches, keep the normal
  browser/search/no-action rules.

Working-context follow-up routing:
- If working_context shows a recently opened/active app and the user asks for
  more specific current information related to that surface, use browser_search
  when the supported templates do not provide a direct app-specific operation.
- Do not reopen the same app just because it is active in working_context.
- If working_context.active_surface is "browser" or working_context.active_browser
  is present and the user asks to open a numbered/ordinal result from the current
  page, choose template_key=browser_select_result with slots.index. Do not choose
  browser_open for this case.

Frontend-supported action templates:
- notification_show, clipboard_copy, clipboard_paste, open_url,
  browser_open, browser_navigate, browser_search, browser_select_result,
  browser_extract_dom, browser_click, browser_type, app_open, app_focus,
  app_close, file_read, file_write, terminal_run, screen_screenshot,
  mouse_click, mouse_drag, keyboard_type, keyboard_hotkey, web_search.
- terminal_run, file_write, mouse_click, mouse_drag, and clipboard_paste require
  confirmation.
"""


_INTENT_GATE_COMPACT_PROMPT = """You are JARVIS Action Intent Gate.
Return only one JSON object:
{"should_act":bool,"intent":string,"template_key":string|null,"slots":object,"confidence":number,"reason":string}

Decide whether the latest user message asks JARVIS to operate the local computer.
Use runtime_context as the source of truth for available apps and capabilities.

Action rules:
- Judge only the latest user message and the supplied runtime_context. Do not
  reuse examples, prior guesses, or unrelated app facts.
- Highest priority: greetings, thanks, casual chat, recommendations,
  explanations, and ordinary information-only questions are no_action even when
  runtime_context lists available apps.
- If the message asks to open/focus/close an app, browse/search/open a page,
  click/type/press keys, use clipboard, read/write files, run terminal, or
  capture the screen, should_act=true.
- Fresh-context app priority: when there is no working_context and an installed
  app in runtime_context.available_applications matches the user's requested task
  by name, alias, capability, category, keyword, or clear domain fit, choose
  template_key=app_open and slots.app_name=<exact available app name>.
- Current/local/live information requests are app-open candidates when an
  installed app advertises a matching capability/category/keyword/alias. Infer
  from runtime_context app facts, not from a hardcoded intent table.
- If working_context shows an active/recent app and the user asks for more
  specific current information related to that surface, choose browser_search
  with slots.query instead of reopening the same app.
- If the active browser has visible search results and the user asks for a
  numbered result, choose browser_select_result with slots.index. The index must
  match the user's ordinal number, not a default first result.

Template keys:
browser_open, browser_search, browser_search_open_first, browser_select_result,
open_url, browser_navigate, browser_extract_dom, browser_click, browser_type,
app_open, app_open_type, app_focus, app_close, file_read, file_write,
terminal_run, screen_screenshot, mouse_click, mouse_drag, keyboard_type,
keyboard_hotkey, clipboard_copy, clipboard_paste, notification_show, web_search.

Required slots:
app_* needs slots.app_name. browser_search/web_search needs slots.query.
browser_select_result needs slots.index. open_url/browser_navigate needs slots.url.
app_open_type/browser_type/keyboard_type/file_write needs slots.text.

If should_act=false, use intent="none", template_key=null, slots={}.
If should_act=true, template_key must be one supported template and all required
slots must be present. Otherwise return should_act=false.
Never invent unavailable apps or unsupported template keys.

Examples:
User: 안녕?
{"should_act":false,"intent":"none","template_key":null,"slots":{},"confidence":0.95,"reason":"ordinary conversation"}
User: 두번째 결과 열어줘
{"should_act":true,"intent":"browser.select_result","template_key":"browser_select_result","slots":{"index":2},"confidence":0.95,"reason":"open visible browser result"}
User: 세번째 결과 열어줘
{"should_act":true,"intent":"browser.select_result","template_key":"browser_select_result","slots":{"index":3},"confidence":0.95,"reason":"open visible browser result"}
"""


def runtime_intent_gate_prompt() -> str:
    if os.getenv(_PROMPTS_YAML_ENV):
        return intent_gate_prompt()
    raw = os.getenv("JARVIS_ACTION_INTENT_COMPACT_PROMPT", "1").strip().lower()
    if raw in {"0", "false", "no"}:
        return intent_gate_prompt()
    return _INTENT_GATE_COMPACT_PROMPT


def intent_gate_prompt() -> str:
    loaded_prompt = _load_prompt_from_yaml(_ACTION_INTENT_GATE_PROMPT_KEY)
    if loaded_prompt and os.getenv(_PROMPTS_YAML_ENV):
        return loaded_prompt
    prompt = loaded_prompt or _INTENT_GATE_PROMPT_FALLBACK
    if "Fresh-context app priority" in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{_INTENT_GATE_POLICY_OVERLAY}"


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
