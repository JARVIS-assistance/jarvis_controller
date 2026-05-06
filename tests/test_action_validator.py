from jarvis_contracts import ClientAction, ClientActionPlan, ClientActionV2
from jarvis_controller.planner.action_validator import ActionValidator


def test_validator_rejects_app_open_abstract_browser_target() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="app.open",
                target="browser",
                description="open browser",
            )
        ],
    )

    result = ActionValidator().validate_plan(plan)

    assert result.valid is False
    assert result.issues[0].code == "abstract_app_target"


def test_validator_rejects_v1_app_control_abstract_browser_target() -> None:
    action = ClientAction(
        type="app_control",
        command="open",
        target="browser",
        args={},
        description="open browser",
        requires_confirm=False,
    )

    result = ActionValidator().validate_v1_actions([action])

    assert result.valid is False
    assert result.issues[0].code == "abstract_app_target"


def test_validator_rejects_disabled_capability() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.search",
                args={"query": "두부조림"},
                description="search",
            )
        ],
    )

    result = ActionValidator().validate_plan(
        plan,
        context={"capabilities": [{"name": "browser.search", "enabled": False}]},
    )

    assert result.valid is False
    assert result.issues[0].code == "disabled_capability"


def test_validator_rejects_unknown_capability() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.teleport",
                args={},
                description="unknown",
            )
        ],
    )

    result = ActionValidator().validate_plan(plan)

    assert result.valid is False
    assert result.issues[0].code == "unknown_action"


def test_validator_forces_confirmation_for_risky_actions() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="terminal.run",
                args={"command": "npm install"},
                description="run command",
                requires_confirm=False,
            )
        ],
    )

    result = ActionValidator().validate_plan(plan)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.actions[0].requires_confirm is True


def test_validator_does_not_rewrite_natural_language_into_actions() -> None:
    plan = ClientActionPlan(
        mode="direct",
        goal="open browser from natural language",
        actions=[],
    )

    result = ActionValidator().validate_plan(plan)

    assert result.valid is False
    assert result.plan is not None
    assert result.plan.actions == []
