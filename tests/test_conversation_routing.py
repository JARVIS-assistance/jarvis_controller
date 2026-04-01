from jarvis_controller.router.conversation_routing import (
    ConversationContext,
    ConversationMode,
    evaluate_conversation_mode,
)


def test_default_short_message_stays_realtime() -> None:
    decision = evaluate_conversation_mode("오늘 날씨 어때?")
    assert decision.mode == ConversationMode.REALTIME
    assert decision.triggered is False


def test_explicit_deep_request_wins() -> None:
    decision = evaluate_conversation_mode("바로 답하지 말고 깊게 생각해서 원인 분석해줘")
    assert decision.mode == ConversationMode.DEEP
    assert decision.triggered is True
    assert "explicit deep-thinking request" in decision.reasons


def test_explicit_planning_request_wins() -> None:
    decision = evaluate_conversation_mode("작업 계획 세워줘. 단계별로 정리해줘.")
    assert decision.mode == ConversationMode.PLANNING
    assert decision.triggered is True
    assert "explicit planning request" in decision.reasons


def test_code_and_analysis_request_escalates_to_deep() -> None:
    decision = evaluate_conversation_mode(
        """
        traceback:
        ValueError: bad state

        이 로그를 보고 원인 분석하고 설계 관점에서 문제를 비교해줘.
        """
    )
    assert decision.mode == ConversationMode.DEEP
    assert decision.triggered is True


def test_multistep_request_escalates_to_planning() -> None:
    decision = evaluate_conversation_mode(
        """
        1. 요구사항 정리
        2. 일정 추정
        3. 체크리스트 작성
        """
    )
    assert decision.mode == ConversationMode.PLANNING
    assert decision.triggered is True


def test_recent_failures_can_push_borderline_query_to_deep() -> None:
    decision = evaluate_conversation_mode(
        "이 문제 원인 좀 분석해줘",
        context=ConversationContext(recent_failures=2),
    )
    assert decision.mode == ConversationMode.DEEP
    assert decision.triggered is True


def test_override_can_force_realtime() -> None:
    decision = evaluate_conversation_mode(
        "깊게 생각해줘",
        override="realtime",
    )
    assert decision.mode == ConversationMode.REALTIME
    assert decision.triggered is False
