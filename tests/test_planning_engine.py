from jarvis_controller.planner.planning_engine import build_plan


def test_build_plan_uses_explicit_steps_when_present() -> None:
    plan = build_plan(
        """
        신규 대화 승격 플로우 정리
        제약: 기존 실시간 응답은 유지
        1. 트리거 조건 정의
        2. 세션 상태 모델링
        3. 검증 방식 정리
        """
    )

    assert plan.goal == "신규 대화 승격 플로우 정리"
    assert plan.constraints == ["제약: 기존 실시간 응답은 유지"]
    assert [step.id for step in plan.steps] == ["s1", "s2", "s3"]
    assert plan.steps[0].description == "트리거 조건 정의"


def test_build_plan_generates_default_steps_without_explicit_list() -> None:
    plan = build_plan("사용자 대화 중 플래닝 모드로 전환하는 구조를 설계해줘")

    assert plan.goal == "사용자 대화 중 플래닝 모드로 전환하는 구조를 설계해줘"
    assert len(plan.steps) == 4
    assert plan.steps[0].title == "Clarify objective"
