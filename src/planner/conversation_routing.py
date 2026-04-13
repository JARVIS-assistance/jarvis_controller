from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ConversationMode(StrEnum):
    REALTIME = "realtime"
    DEEP = "deep"
    PLANNING = "planning"


@dataclass(slots=True)
class ConversationContext:
    recent_failures: int = 0
    ambiguity_count: int = 0
    turn_index: int = 0
    active_mode: ConversationMode = ConversationMode.REALTIME


@dataclass(slots=True)
class RoutingDecision:
    mode: ConversationMode
    triggered: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)


EXPLICIT_DEEP_PHRASES = {
    "깊게 생각",
    "깊이 생각",
    "deep think",
    "think deeply",
    "analyze carefully",
    "carefully analyze",
}

EXPLICIT_PLANNING_PHRASES = {
    "계획",
    "플랜",
    "단계별",
    "step by step plan",
    "make a plan",
    "plan this",
}

ANALYSIS_KEYWORDS = {
    "원인",
    "분석",
    "설계",
    "디버깅",
    "리팩터링",
    "최적화",
    "비교",
    "아키텍처",
    "strategy",
    "debug",
    "analysis",
    "design",
    "refactor",
    "optimize",
    "compare",
}

EXECUTION_KEYWORDS = {
    # 앱/프로그램 제어
    "실행", "열어", "켜줘", "띄워", "종료", "닫아",
    "launch", "open", "run", "start", "close", "quit",
    # 파일 작업
    "파일 만들어", "파일 생성", "파일 저장", "파일 읽어",
    "create file", "write file", "save file",
    # 터미널/명령
    "터미널", "명령어", "설치", "빌드", "배포",
    "terminal", "command", "install", "build", "deploy",
    # 물리 제어
    "클릭", "드래그", "스크롤", "타이핑", "입력",
    "스크린샷", "화면 캡처", "화면 보여",
    "click", "drag", "scroll", "type", "screenshot",
    "hotkey", "단축키",
}

SEARCH_KEYWORDS = {
    # 검색 직접 요청
    "검색", "찾아", "찾아줘", "찾아봐", "검색해", "검색해줘", "서치",
    "search", "look up", "find",
    # 정보/지식 요청
    "알려줘", "알려줄래", "알려주세요", "알아봐", "알아봐줘",
    "뭐야", "뭔가요", "어때", "어떤가요",
    "what is", "what's", "how to", "how much", "how many",
    "who is", "when is", "where is", "tell me",
    # 날씨/뉴스/시세 등 실시간 정보
    "날씨", "기온", "온도", "미세먼지",
    "weather", "temperature", "forecast",
    "뉴스", "소식", "속보", "news",
    "주가", "환율", "시세", "코인", "비트코인",
    "stock", "price", "exchange rate",
    # 번역/사전
    "번역", "뜻", "의미", "translate", "meaning",
}

PLANNING_KEYWORDS = {
    "로드맵",
    "순서",
    "체크리스트",
    "마일스톤",
    "일정",
    "task breakdown",
    "roadmap",
    "checklist",
    "milestone",
}


def evaluate_conversation_mode(
    message: str,
    *,
    override: str | None = None,
    context: ConversationContext | None = None,
) -> RoutingDecision:
    normalized = _normalize(message)
    ctx = context or ConversationContext()

    if override:
        mode = ConversationMode(override)
        return RoutingDecision(
            mode=mode,
            triggered=mode is not ConversationMode.REALTIME,
            confidence=1.0,
            reasons=[f"explicit override: {mode.value}"],
        )

    if _contains_phrase(normalized, EXPLICIT_DEEP_PHRASES):
        return RoutingDecision(
            mode=ConversationMode.DEEP,
            triggered=True,
            confidence=0.99,
            reasons=["explicit deep-thinking request"],
        )

    if _contains_phrase(normalized, EXPLICIT_PLANNING_PHRASES):
        return RoutingDecision(
            mode=ConversationMode.PLANNING,
            triggered=True,
            confidence=0.99,
            reasons=["explicit planning request"],
        )

    deep_score = 0
    planning_score = 0
    reasons: list[str] = []

    if len(normalized) >= 400:
        deep_score += 2
        reasons.append("long-form request")

    if "```" in message or _looks_like_log_or_traceback(message):
        deep_score += 2
        reasons.append("code/log payload detected")

    analysis_hits = _count_keyword_hits(normalized, ANALYSIS_KEYWORDS)
    if analysis_hits:
        deep_score += min(analysis_hits, 3)
        reasons.append("analysis-oriented language")

    execution_hits = _count_keyword_hits(normalized, EXECUTION_KEYWORDS)
    if execution_hits:
        deep_score += min(execution_hits, 3)
        reasons.append("execution/control-oriented language")

    search_hits = _count_keyword_hits(normalized, SEARCH_KEYWORDS)
    if search_hits:
        deep_score += min(search_hits, 3)
        reasons.append("search/information-oriented language")

    planning_hits = _count_keyword_hits(normalized, PLANNING_KEYWORDS)
    if planning_hits:
        planning_score += min(planning_hits, 3)
        reasons.append("planning-oriented language")

    if _has_multistep_structure(message):
        planning_score += 2
        reasons.append("multi-step objective")

    if ctx.recent_failures > 0:
        deep_score += min(ctx.recent_failures, 2)
        reasons.append("recent realtime failure")

    if ctx.ambiguity_count > 1:
        planning_score += 1
        reasons.append("conversation ambiguity accumulated")

    # 실행/제어/검색 키워드가 있으면 임계값 낮춤 (짧은 명령도 deep으로)
    deep_threshold = 1 if (execution_hits or search_hits) else 4

    if deep_score >= deep_threshold and deep_score >= planning_score:
        return RoutingDecision(
            mode=ConversationMode.DEEP,
            triggered=True,
            confidence=_confidence_from_score(deep_score),
            reasons=reasons,
        )

    if planning_score >= 3:
        return RoutingDecision(
            mode=ConversationMode.PLANNING,
            triggered=True,
            confidence=_confidence_from_score(planning_score),
            reasons=reasons,
        )

    return RoutingDecision(
        mode=ConversationMode.REALTIME,
        triggered=False,
        confidence=0.7,
        reasons=["stay in realtime mode"],
    )


def _normalize(message: str) -> str:
    return " ".join(message.lower().split())


def _contains_phrase(message: str, phrases: set[str]) -> bool:
    return any(phrase in message for phrase in phrases)


def _count_keyword_hits(message: str, keywords: set[str]) -> int:
    return sum(1 for keyword in keywords if keyword in message)


def _looks_like_log_or_traceback(message: str) -> bool:
    tokens = ("traceback", "exception", "error:", "stack trace", "stderr", "stdout")
    lowered = message.lower()
    return any(token in lowered for token in tokens)


def _has_multistep_structure(message: str) -> bool:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    numbered_lines = sum(line[:2] in {"1.", "2.", "3.", "4.", "5."} for line in lines)
    conjunctions = ("그리고", "다음", "then", "after that", "finally")
    lowered = message.lower()
    return numbered_lines >= 2 or sum(token in lowered for token in conjunctions) >= 2


def _confidence_from_score(score: int) -> float:
    return min(0.55 + (score * 0.1), 0.95)
