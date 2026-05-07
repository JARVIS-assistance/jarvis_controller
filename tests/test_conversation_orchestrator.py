from jarvis_controller.planner.conversation_orchestrator import orchestrate_conversation_turn
from jarvis_controller.planner.conversation_routing import ConversationMode


class StubCoreClient:
    def run_realtime_conversation(self, message: str):
        from jarvis_controller.middleware.core_client import CoreResponse

        return CoreResponse(
            mode="realtime",
            summary="stub realtime",
            content=f"realtime:{message}",
            next_actions=[],
        )

    def run_deep_thinking(self, message: str):
        from jarvis_controller.middleware.core_client import CoreResponse

        return CoreResponse(
            mode="deep",
            summary="stub deep",
            content="Deep thinking result",
            next_actions=[],
        )


def test_orchestrator_builds_plan_inside_controller() -> None:
    result = orchestrate_conversation_turn(
        """
        작업 계획 세워줘.
        1. 트리거 정리
        2. 상태 전이 정의
        3. 테스트 추가
        """,
        core_client=StubCoreClient(),
    )

    assert result.decision.mode == ConversationMode.PLANNING
    assert result.planning_result is not None
    assert result.core_result is None
    assert result.handler == "jarvis-controller"
    assert result.planning_result.steps[1].description == "상태 전이 정의"


def test_orchestrator_uses_core_for_deep_mode() -> None:
    result = orchestrate_conversation_turn(
        "이 에러 로그를 보고 깊게 생각해서 원인 분석해줘\nTraceback: bad state",
        core_client=StubCoreClient(),
    )

    assert result.decision.mode == ConversationMode.DEEP
    assert result.planning_result is None
    assert result.core_result is not None
    assert result.handler == "jarvis-core"
    assert "Deep thinking result" in result.core_result.content
