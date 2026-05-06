from jarvis_contracts import ClientActionPlan, ClientActionV2

from jarvis_controller.planner.action_adapter import V2ToV1ActionAdapter
from jarvis_controller.planner.action_compiler import _parse_plan
from jarvis_controller.planner.action_compiler import parse_embedded_actions_from_text


def test_adapter_maps_browser_search_to_deterministic_search_url() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.search",
                args={"query": "두부조림 레시피"},
                description="search",
            )
        ],
    )

    result = V2ToV1ActionAdapter().adapt_plan(
        plan,
        context={"default_browser": "chrome"},
    )

    assert result.valid is True
    action = result.actions[0]
    assert action.type == "open_url"
    assert action.target == (
        "https://www.google.com/search?q=%EB%91%90%EB%B6%80%EC%A1%B0%EB%A6%BC+"
        "%EB%A0%88%EC%8B%9C%ED%94%BC"
    )
    assert action.args["query"] == "두부조림 레시피"
    assert action.args["browser"] == "chrome"


def test_adapter_uses_runtime_search_engine_for_browser_search() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.search",
                args={"query": "연어장"},
                description="search",
            )
        ],
    )

    result = V2ToV1ActionAdapter().adapt_plan(
        plan,
        context={"default_browser": "chrome", "search_engine": "naver"},
    )

    assert result.valid is True
    action = result.actions[0]
    assert action.type == "open_url"
    assert action.target == (
        "https://search.naver.com/search.naver?query=%EC%97%B0%EC%96%B4%EC%9E%A5"
    )
    assert action.args["query"] == "연어장"


def test_adapter_maps_browser_navigate_to_open_url() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="browser.navigate",
                args={"url": "https://example.com"},
                description="navigate",
            )
        ],
    )

    result = V2ToV1ActionAdapter().adapt_plan(plan)

    assert result.valid is True
    assert result.actions[0].type == "open_url"
    assert result.actions[0].target == "https://example.com"


def test_adapter_maps_concrete_app_open_to_app_control() -> None:
    plan = ClientActionPlan(
        mode="direct",
        actions=[
            ClientActionV2(
                name="app.open",
                target="Sublime Text",
                args={"bundle_id": "com.sublimetext.4"},
                description="open app",
            )
        ],
    )

    result = V2ToV1ActionAdapter().adapt_plan(plan)

    assert result.valid is True
    assert result.actions[0].type == "app_control"
    assert result.actions[0].command == "open"
    assert result.actions[0].target == "Sublime Text"


def test_adapter_rejects_app_open_browser() -> None:
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

    result = V2ToV1ActionAdapter().adapt_plan(plan)

    assert result.valid is False
    assert result.actions == []
    assert result.issues[0].code == "abstract_app_target"


def test_compiler_parses_legacy_open_url_query_as_browser_search_plan() -> None:
    plan = _parse_plan(
        """
        {
          "should_act": true,
          "execution_mode": "direct",
          "intent": "browser_search",
          "confidence": 0.9,
          "reason": "legacy",
          "actions": [
            {
              "type": "open_url",
              "command": null,
              "target": "https://www.google.com/search?q=salmon",
              "args": {"query": "salmon", "browser": "chrome"},
              "description": "Search salmon",
              "requires_confirm": false
            }
          ]
        }
        """
    )

    assert plan.actions[0].name == "browser.search"
    assert plan.actions[0].args["query"] == "salmon"


def test_embedded_legacy_browser_app_query_recovers_as_search_action() -> None:
    result = parse_embedded_actions_from_text(
        """
        ```actions
        {"type":"app_control","command":"open","target":"browser","args":{"query":"연어장","browser":"chrome"},"description":"search","requires_confirm":false}
        ```
        """,
        context={"capabilities": ["browser.search"], "search_engine": "naver"},
    )

    assert result.saw_action_block is True
    assert result.issues == []
    assert len(result.actions) == 1
    assert result.actions[0].type == "open_url"
    assert result.actions[0].target == (
        "https://search.naver.com/search.naver?query=%EC%97%B0%EC%96%B4%EC%9E%A5"
    )


def test_embedded_app_browser_plus_web_search_recovers_structured_query() -> None:
    result = parse_embedded_actions_from_text(
        """
        ```actions
        [
          {"type": "app_control", "command": "open", "target": "browser"},
          {"type": "web_search", "query": "연어장"}
        ]
        ```
        """,
        context={
            "capabilities": ["browser.search"],
            "default_browser": "chrome",
            "search_engine": "naver",
        },
    )

    assert result.saw_action_block is True
    assert len(result.actions) == 1
    assert result.actions[0].type == "open_url"
    assert result.actions[0].target == (
        "https://search.naver.com/search.naver?query=%EC%97%B0%EC%96%B4%EC%9E%A5"
    )
    assert result.actions[0].args["browser"] == "chrome"


def test_embedded_web_click_search_result_recovers_select_result() -> None:
    result = parse_embedded_actions_from_text(
        """
        ```actions
        [
          {"type": "web_click", "target": "search_result_2"}
        ]
        ```
        """,
        context={"capabilities": ["browser.select_result"]},
    )

    assert result.saw_action_block is True
    assert result.issues == []
    assert len(result.actions) == 1
    assert result.actions[0].type == "browser_control"
    assert result.actions[0].command == "select_result"
    assert result.actions[0].args["index"] == 2
