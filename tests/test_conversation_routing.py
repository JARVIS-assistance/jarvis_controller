from planner.action_intent_classifier import (
    classify_client_action_intent_decision,
    coerce_client_actions_from_text,
    should_try_client_action_classifier,
)
from planner.conversation_routing import (
    ConversationContext,
    ConversationMode,
    evaluate_conversation_mode,
)


def test_default_short_message_stays_realtime() -> None:
    decision = evaluate_conversation_mode("안녕, 지금 대답 가능해?")
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


def test_non_empty_message_uses_model_classifier_without_keyword_gate() -> None:
    assert should_try_client_action_classifier("지금 대답속도 테스트해보는 중") is True


def test_empty_message_skips_client_action_classifier() -> None:
    assert should_try_client_action_classifier("   ") is False


def test_plain_message_returns_model_no_action(monkeypatch) -> None:
    monkeypatch.setattr(
        "planner.action_compiler._post_json",
        lambda *args, **kwargs: {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"mode":"no_action","goal":null,'
                            '"confidence":0.94,"reason":"ordinary chat",'
                            '"actions":[]}'
                        )
                    }
                }
            ]
        },
    )

    decision = classify_client_action_intent_decision("지금 대답속도 테스트해보는 중")
    assert decision is not None
    assert decision.should_act is False
    assert decision.execution_mode == "no_action"
    assert decision.reason == "ordinary chat"


def test_model_unavailable_uses_conservative_browser_search_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "planner.action_compiler._post_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
    )

    decision = classify_client_action_intent_decision("크롬에서 openai 검색해줘")

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct"
    assert decision.actions[0].type == "open_url"
    assert decision.actions[0].args["query"] == "openai"
    assert decision.actions[0].args["browser"] == "chrome"


def test_model_unavailable_uses_app_open_and_korean_type_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "planner.action_compiler._post_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
    )

    decision = classify_client_action_intent_decision("sublime text 켜서 안녕하세요 작성해줘")

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct_sequence"
    assert [action.type for action in decision.actions] == ["app_control", "keyboard_type"]
    assert decision.actions[0].target == "Sublime Text"
    assert decision.actions[1].payload == "안녕하세요"


def test_model_unavailable_uses_search_result_selection_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "planner.action_compiler._post_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
    )

    decision = classify_client_action_intent_decision("두번째 검색결과 들어가줘")

    assert decision is not None
    assert decision.should_act is True
    assert decision.actions[0].type == "browser_control"
    assert decision.actions[0].command == "select_result"
    assert decision.actions[0].args["index"] == 2


def test_embedded_abstract_browser_app_action_is_not_dispatchable() -> None:
    actions = coerce_client_actions_from_text(
        """```actions
        {"type":"app_control","command":"open","target":"browser","args":{},"description":"x","requires_confirm":false}
        ```""",
        message="브라우저 켜서 연어장 찾아줘",
    )

    assert actions == []
