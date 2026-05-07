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


def test_validator_accepts_v1_browser_open() -> None:
    action = ClientAction(
        type="browser",
        command="open",
        target=None,
        args={"browser": "safari"},
        description="open browser",
        requires_confirm=False,
    )

    result = ActionValidator().validate_v1_actions(
        [action],
        context={"capabilities": [{"name": "browser.open", "enabled": True}]},
    )

    assert result.valid is True


def test_validator_rejects_app_target_outside_runtime_profile() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="app.open",
                target="chrome",
                description="open browser",
            )
        ],
    )

    result = ActionValidator().validate_plan(
        plan,
        context={"available_applications": [{"name": "Google Chrome"}]},
    )

    assert result.valid is False
    assert result.issues[0].code == "unknown_application_target"
    assert result.issues[0].details["available_applications"] == ["Google Chrome"]


def test_validator_accepts_exact_runtime_profile_app_name() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="app.open",
                target="Google Chrome",
                description="open browser",
            )
        ],
    )

    result = ActionValidator().validate_plan(
        plan,
        context={"available_applications": [{"name": "Google Chrome"}]},
    )

    assert result.valid is True


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


def test_validator_rejects_browser_click_non_positive_ai_id() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.click",
                args={"ai_id": 0},
                description="click element",
            )
        ],
    )

    result = ActionValidator().validate_plan(plan)

    assert result.valid is False
    assert result.issues[0].code == "invalid_argument"
    assert result.issues[0].action_index == 0
    assert result.issues[0].action_name == "browser.click"
    assert result.issues[0].field == "args.ai_id"
    assert result.issues[0].details == {"value": 0, "minimum": 1}


def test_validator_rejects_browser_type_non_positive_ai_id() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.type",
                args={"ai_id": 0, "text": "hello"},
                description="type text",
            )
        ],
    )

    result = ActionValidator().validate_plan(plan)

    assert result.valid is False
    assert result.issues[0].code == "invalid_argument"
    assert result.issues[0].action_name == "browser.type"
    assert result.issues[0].field == "args.ai_id"


def test_validator_rejects_browser_select_result_non_positive_index() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.select_result",
                args={"index": 0},
                description="select first result",
            )
        ],
    )

    result = ActionValidator().validate_plan(plan)

    assert result.valid is False
    assert result.issues[0].code == "invalid_argument"
    assert result.issues[0].action_index == 0
    assert result.issues[0].action_name == "browser.select_result"
    assert result.issues[0].field == "args.index"
    assert result.issues[0].details == {"value": 0, "minimum": 1}


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
