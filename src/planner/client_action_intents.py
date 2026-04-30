from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from jarvis_contracts import ClientAction
from planner.action_intent_classifier import classify_client_action_intent


@dataclass(frozen=True)
class ActionIntentRule:
    name: str
    build: Callable[[str, dict[str, Any] | None], list[ClientAction]]


def infer_client_actions(
    message: str,
    *,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    """Infer immediate client-side actions from short control commands.

    This is intentionally conservative: rules should only fire when the user is
    clearly asking the client runtime to perform an external action.
    """
    for rule in FAST_ACTION_INTENT_RULES:
        actions = rule.build(message, context)
        if actions:
            return actions
    classified = classify_client_action_intent(message, context=context)
    if classified:
        return classified
    for rule in FALLBACK_ACTION_INTENT_RULES:
        actions = rule.build(message, context)
        if actions:
            return actions
    return []


def _browser_result_selection(
    message: str,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    text = message.lower()
    result_index = _extract_result_index(text)
    wants_select = _has_any(
        text,
        "선택",
        "클릭",
        "들어가",
        "들어가줘",
        "열어",
        "열어줘",
        "open",
        "click",
        "select",
    )
    if result_index is None or not wants_select:
        return []
    return [
        ClientAction(
            type="browser_control",
            command="select_result",
            target="active_tab",
            args={"index": result_index},
            description=f"현재 브라우저 검색 결과에서 {result_index}번째 항목 선택",
            requires_confirm=False,
        )
    ]


def _browser_search_or_open(
    message: str,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    text = message.lower()
    wants_browser = _has_any(
        text,
        "browser",
        "브라우저",
        "chrome",
        "크롬",
        "safari",
        "사파리",
        "웹",
    )
    wants_search = _has_any(
        text,
        "검색",
        "search",
        "찾아",
        "찾아줘",
        "찾아봐",
        "알려줘",
        "레시피",
        "recipe",
    )
    wants_open = _has_any(
        text,
        "켜",
        "켜서",
        "열어",
        "열어서",
        "open",
        "launch",
    )
    url = _extract_url(message)

    if url and (wants_browser or wants_open):
        return [
            ClientAction(
                type="open_url",
                command=None,
                target=url,
                args={"browser": _browser_name(text)},
                description=f"브라우저에서 URL 열기: {url}",
                requires_confirm=False,
            )
        ]

    if not wants_browser or not (wants_search or wants_open):
        return []

    query = _extract_browser_search_query(message)
    target = (
        f"https://www.google.com/search?q={quote_plus(query)}"
        if query
        else "https://www.google.com"
    )
    return [
        ClientAction(
            type="open_url",
            command=None,
            target=target,
            args={"browser": _browser_name(text), "query": query},
            description=(
                f"브라우저에서 '{query}' 검색"
                if query
                else "브라우저에서 Google 열기"
            ),
            requires_confirm=False,
        )
    ]


def _browser_open_from_current_page(
    message: str,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    text = message.lower()
    has_browser_context = bool((context or {}).get("browser_active"))
    wants_current_page = _has_any(
        text,
        "지금 브라우저",
        "브라우저에서",
        "크롬에서",
        "현재 페이지",
        "이 페이지",
        "여기서",
        "페이지에서",
        "검색 결과에서",
        "current page",
        "this page",
    )
    wants_open = _has_any(
        text,
        "열어",
        "열어줘",
        "들어가",
        "들어가줘",
        "선택",
        "클릭",
        "open",
        "click",
        "select",
    )
    if not (wants_current_page or has_browser_context) or not wants_open:
        return []
    query = _extract_current_page_open_query(message)
    if not query:
        return []
    return [
        ClientAction(
            type="browser_control",
            command="extract_dom",
            target="active_tab",
            args={
                "purpose": "resolve_open_request",
                "query": query,
                "include_links": True,
                "max_links": 120,
            },
            description=f"현재 페이지 DOM에서 '{query}' 링크 후보 추출",
            requires_confirm=False,
        )
    ]


def _browser_navigation(
    message: str,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    text = message.lower()
    command: str | None = None
    description: str | None = None
    if _has_any(text, "뒤로", "이전", "back"):
        command = "back"
        description = "브라우저에서 뒤로 이동"
    elif _has_any(text, "앞으로", "forward"):
        command = "forward"
        description = "브라우저에서 앞으로 이동"
    elif _has_any(text, "새로고침", "reload", "refresh"):
        command = "reload"
        description = "브라우저 새로고침"
    if command is None:
        return []
    return [
        ClientAction(
            type="browser_control",
            command=command,
            target="active_tab",
            args={},
            description=description or "브라우저 제어",
            requires_confirm=False,
        )
    ]


def _browser_scroll(
    message: str,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    text = message.lower()
    if not _has_any(text, "scroll", "스크롤", "내려", "아래", "page down", "올려", "위로"):
        return []
    direction = "up" if _has_any(text, "올려", "위로", "up", "page up") else "down"
    description = (
        "현재 브라우저 페이지를 위로 스크롤"
        if direction == "up"
        else "현재 브라우저 페이지를 아래로 스크롤"
    )
    return [
        ClientAction(
            type="browser_control",
            command="scroll",
            target="active_tab",
            args={"direction": direction, "amount": "page"},
            description=description,
            requires_confirm=False,
        )
    ]


def _app_launch(
    message: str,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    text = message.lower()
    if not _has_any(text, "켜", "열어", "실행", "launch", "open"):
        return []
    app_name = _extract_app_name(text)
    if app_name is None:
        return []
    return [
        ClientAction(
            type="app_control",
            command="open",
            target=app_name,
            args={},
            description=f"{app_name} 실행",
            requires_confirm=False,
        )
    ]


def _app_launch_and_type(
    message: str,
    context: dict[str, Any] | None = None,
) -> list[ClientAction]:
    text = message.lower()
    if not _has_any(text, "켜서", "열어서", "실행해서", "open", "launch"):
        return []
    if not _has_any(text, "작성", "입력", "써", "타이핑", "write", "type"):
        return []
    app_name = _extract_app_name(text)
    typed_text = _extract_text_to_type(message)
    if app_name is None or not typed_text:
        return []
    return [
        ClientAction(
            type="app_control",
            command="open",
            target=app_name,
            args={},
            description=f"{app_name} 실행",
            requires_confirm=False,
        ),
        ClientAction(
            type="keyboard_type",
            command=None,
            target=None,
            payload=typed_text,
            args={"enter": False},
            description=f"{app_name}에 텍스트 입력",
            requires_confirm=False,
        ),
    ]


FAST_ACTION_INTENT_RULES: tuple[ActionIntentRule, ...] = (
    ActionIntentRule("browser_result_selection", _browser_result_selection),
    ActionIntentRule("browser_navigation", _browser_navigation),
    ActionIntentRule("browser_scroll", _browser_scroll),
    ActionIntentRule("app_launch_and_type", _app_launch_and_type),
)

FALLBACK_ACTION_INTENT_RULES: tuple[ActionIntentRule, ...] = (
    ActionIntentRule("browser_open_from_current_page", _browser_open_from_current_page),
    ActionIntentRule("browser_search_or_open", _browser_search_or_open),
    ActionIntentRule("app_launch", _app_launch),
)


def _has_any(text: str, *tokens: str) -> bool:
    return any(token in text for token in tokens)


def _extract_url(message: str) -> str | None:
    match = re.search(r"https?://\S+", message)
    if not match:
        return None
    return match.group(0).rstrip(".,!?)")


def _browser_name(text: str) -> str:
    if _has_any(text, "safari", "사파리"):
        return "safari"
    if _has_any(text, "firefox", "파이어폭스"):
        return "firefox"
    return "chrome"


def _extract_app_name(text: str) -> str | None:
    aliases = {
        "chrome": "chrome",
        "크롬": "chrome",
        "browser": "browser",
        "브라우저": "browser",
        "safari": "safari",
        "사파리": "safari",
        "firefox": "firefox",
        "파이어폭스": "firefox",
        "sublimetext": "Sublime Text",
        "sublime text": "Sublime Text",
        "sublime": "Sublime Text",
        "서브라임": "Sublime Text",
        "메모장": "TextEdit",
        "textedit": "TextEdit",
    }
    for token, app_name in aliases.items():
        if token in text:
            return app_name
    return None


def _extract_browser_search_query(message: str) -> str:
    text = message.strip()
    patterns = [
        r"https?://\S+",
        r"\b(browser|chrome|safari|firefox|web)\b",
        r"(브라우저|크롬|사파리|파이어폭스|웹)",
        r"(켜서|열어서|켜줘|열어줘|켜|열어|실행해서|실행)",
        r"(검색해줘|검색해주|검색해|검색|찾아줘|찾아주|찾아봐|찾아|알려줘|알려주|알려)",
        r"(해줘|줘|좀|에서|으로|로|해서)",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,!?\n\t")
    return text


def _extract_text_to_type(message: str) -> str:
    text = message.strip()
    text = re.sub(
        r"(?i)\b(sublime\s*text|sublimetext|sublime|textedit|notepad)\b",
        " ",
        text,
    )
    patterns = [
        r"(서브라임|메모장|앱|어플)",
        r"(켜서|열어서|실행해서|켜줘|열어줘|실행해줘|켜|열어|실행)",
        r"(작성가능해줘|작성해줘|작성해주|작성|입력해줘|입력해주|입력|써줘|써주|써|타이핑해줘|타이핑)",
        r"(해줘|해주|줘|주|가능)",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" .,!?\n\t")


def _extract_current_page_open_query(message: str) -> str:
    text = message.strip()
    patterns = [
        r"(지금\s*브라우저|브라우저에서|크롬에서)",
        r"(현재\s*페이지|이\s*페이지|여기서|페이지에서|검색\s*결과에서)",
        r"\b(current page|this page)\b",
        r"(열어줘|열어주|열어|들어가줘|들어가주|들어가|선택해줘|선택해주|선택|클릭해줘|클릭해주|클릭)",
        r"\b(open|click|select)\b",
        r"(지금|해줘|해주|줘|주|좀|에서|으로|로|를|을)",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,!?\n\t")
    return text


def _extract_result_index(text: str) -> int | None:
    ordinal_words = {
        "첫번째": 1,
        "첫 번째": 1,
        "첫째": 1,
        "1번째": 1,
        "1번": 1,
        "두번째": 2,
        "두 번째": 2,
        "둘째": 2,
        "2번째": 2,
        "2번": 2,
        "세번째": 3,
        "세 번째": 3,
        "셋째": 3,
        "3번째": 3,
        "3번": 3,
        "네번째": 4,
        "네 번째": 4,
        "넷째": 4,
        "4번째": 4,
        "4번": 4,
        "다섯번째": 5,
        "다섯 번째": 5,
        "5번째": 5,
        "5번": 5,
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
    }
    for token, index in ordinal_words.items():
        if token in text:
            return index
    match = re.search(r"\b([1-9])\s*(?:st|nd|rd|th|번째|번)\b", text)
    if match:
        return int(match.group(1))
    return None
