import json

from planner.action_compiler import (
    ActionCompiler,
    _intent_gate_prompt,
    _parse_intent_gate,
    _parse_plan,
)


def test_action_intent_gate_uses_fast_model_defaults(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL_NAME", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL", raising=False)
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")

    captured: dict[str, object] = {}

    def fake_post_json(url, payload, *, timeout):
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "should_act": False,
                                "intent": "none",
                                "confidence": 0.95,
                                "reason": "test no action",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    gate = ActionCompiler().compile_intent_gate(message="브라우저 열어줘")

    assert gate is not None
    assert gate.should_act is False
    assert captured["timeout"] == 2.0
    assert captured["payload"]["model"] == "docker.io/ai/qwen2.5:1.5B-F16"


def test_action_compiler_uses_gemma_only_after_action_intent(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_COMPILER_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL_NAME", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_COMPILER_MODEL_NAME", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_COMPILER_MODEL", raising=False)
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "browser.open",
                                    "confidence": 0.91,
                                    "reason": "action request",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct",
                                "goal": "Open browser",
                                "confidence": 0.9,
                                "reason": "open browser",
                                "actions": [
                                    {
                                        "name": "browser.search",
                                        "target": None,
                                        "args": {"query": "연어장"},
                                        "description": "search browser",
                                        "requires_confirm": False,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(message="브라우저 열어줘")

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct"
    assert len(calls) == 2
    assert calls[0]["payload"]["model"] == "docker.io/ai/qwen2.5:1.5B-F16"
    assert calls[1]["payload"]["model"] == "docker.io/ai/gemma4:E4B"
    compiler_input = json.loads(calls[1]["payload"]["messages"][-1]["content"])
    assert "browser_open" in compiler_input["action_templates"]
    assert "screen_screenshot" in compiler_input["action_templates"]
    assert calls[0]["timeout"] == 2.0
    assert calls[1]["timeout"] == 20.0


def test_action_compiler_stops_when_intent_gate_returns_no_action(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    calls = 0

    def fake_post_json(url, payload, *, timeout):
        nonlocal calls
        calls += 1
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "should_act": False,
                                "intent": "none",
                                "confidence": 0.97,
                                "reason": "ordinary conversation",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(message="안녕?")

    assert decision is not None
    assert decision.should_act is False
    assert decision.execution_mode == "no_action"
    assert calls == 1


def test_action_compiler_rechecks_low_confidence_no_action_gate(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    monkeypatch.setenv("JARVIS_ACTION_INTENT_CONFIDENCE_THRESHOLD", "0.72")
    calls = 0

    def fake_post_json(url, payload, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": False,
                                    "intent": "none",
                                    "confidence": 0.0,
                                    "reason": "uncertain",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct",
                                "goal": "Open browser",
                                "confidence": 0.9,
                                "reason": "open browser",
                                "actions": [
                                    {
                                        "name": "browser.open",
                                        "args": {},
                                        "description": "open browser",
                                        "requires_confirm": False,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(message="브라우저 열어줘")

    assert decision is not None
    assert decision.should_act is True
    assert decision.actions[0].type == "browser"
    assert calls == 2


def test_action_compiler_accepts_yaml_style_plan_after_gate_fallback(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls = 0

    def fake_post_json(url, payload, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "not json",
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": """
mode: direct
goal: Open browser
confidence: 0.9
reason: open browser
actions:
  - name: browser.open
    args: {}
    description: open browser
    requires_confirm: false
""",
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(message="브라우저 열어줘")

    assert decision is not None
    assert decision.should_act is True
    assert decision.actions[0].type == "browser"


def test_action_compiler_can_use_hosted_ollama_generate(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_ENDPOINT", "https://ollama.example.com")
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL_NAME", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_INTENT_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_COMPILER_MODEL_NAME", raising=False)
    monkeypatch.delenv("JARVIS_ACTION_COMPILER_MODEL", raising=False)
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "response": json.dumps(
                    {
                        "should_act": True,
                        "intent": "browser.search",
                        "confidence": 0.9,
                        "reason": "action request",
                    }
                )
            }
        return {
            "response": json.dumps(
                {
                    "mode": "direct",
                    "goal": "Search",
                    "confidence": 0.9,
                    "reason": "search",
                    "actions": [
                        {
                            "name": "browser.search",
                            "args": {"query": "연어장"},
                            "description": "search browser",
                            "requires_confirm": False,
                        }
                    ],
                }
            )
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(message="브라우저에서 연어장 찾아줘")

    assert decision is not None
    assert decision.should_act is True
    assert len(calls) == 2
    assert calls[0]["url"] == "https://ollama.example.com/api/generate"
    assert calls[0]["payload"]["model"] == "docker.io/ai/qwen2.5:1.5B-F16"
    assert calls[1]["payload"]["model"] == "docker.io/ai/gemma4:E4B"
    assert calls[0]["payload"]["stream"] is False
    assert "System:" in calls[0]["payload"]["prompt"]


def test_action_compiler_can_use_hosted_ollama_chat(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "ollama_chat")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_ENDPOINT", "https://ollma.breakpack.cc")
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_NAME", "qwen2.5:1.5b")
    monkeypatch.setenv("JARVIS_ACTION_COMPILER_MODEL_NAME", "qwen2.5:7b")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "should_act": True,
                            "intent": "browser.search",
                            "confidence": 0.9,
                            "reason": "action request",
                        }
                    ),
                }
            }
        return {
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "mode": "direct",
                        "goal": "Search",
                        "confidence": 0.9,
                        "reason": "search",
                        "actions": [
                            {
                                "name": "browser.search",
                                "args": {"query": "연어장"},
                                "description": "search browser",
                                "requires_confirm": False,
                            }
                        ],
                    }
                ),
            }
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(message="브라우저에서 연어장 찾아줘")

    assert decision is not None
    assert decision.should_act is True
    assert len(calls) == 2
    assert calls[0]["url"] == "https://ollma.breakpack.cc/chat"
    assert calls[0]["payload"]["model"] == "qwen2.5:1.5b"
    assert calls[1]["payload"]["model"] == "qwen2.5:7b"
    assert calls[0]["payload"]["stream"] is False
    assert isinstance(calls[0]["payload"]["messages"], list)


def test_ollama_chat_empty_response_retries_generate(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "ollama_chat")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_ENDPOINT", "https://ollma.breakpack.cc")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "should_act": True,
                            "intent": "web_search",
                            "confidence": 0.95,
                            "reason": "search action",
                        }
                    ),
                }
            }
        if len(calls) == 2:
            return {"message": {"role": "assistant", "content": ""}}
        return {
            "response": json.dumps(
                {
                    "mode": "direct_sequence",
                    "goal": "Search and open result",
                    "confidence": 0.9,
                    "reason": "generate fallback",
                    "actions": [
                        {
                            "name": "browser.search",
                            "args": {"query": "네이버 웹툰"},
                            "description": "Search for Naver Webtoon",
                            "requires_confirm": False,
                        },
                        {
                            "name": "browser.select_result",
                            "args": {"index": 1},
                            "description": "Open first result",
                            "requires_confirm": False,
                        },
                    ],
                }
            )
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="네이버 웹툰 검색해서 들어가 줄래?",
        context={"capabilities": ["browser.search", "browser.select_result"]},
    )

    assert decision is not None
    assert decision.should_act is True
    assert [action.type for action in decision.actions] == ["open_url", "browser_control"]
    assert decision.actions[0].args["query"] == "네이버 웹툰"
    assert decision.actions[1].command == "select_result"
    assert len(calls) == 3
    assert calls[1]["url"] == "https://ollma.breakpack.cc/chat"
    assert calls[2]["url"] == "https://ollma.breakpack.cc/api/generate"


def test_action_compiler_tries_plan_when_intent_gate_returns_invalid_text(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "ollama_chat")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_ENDPOINT", "https://ollma.breakpack.cc")
    calls: list[str] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append(str(payload["model"]))
        if len(calls) == 1:
            return {"message": {"role": "assistant", "content": "I cannot help with that."}}
        return {
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "mode": "direct",
                        "goal": "Search",
                        "confidence": 0.9,
                        "reason": "search",
                        "actions": [
                            {
                                "name": "browser.search",
                                "args": {"query": "연어장"},
                                "description": "search browser",
                                "requires_confirm": False,
                            }
                        ],
                    }
                ),
            }
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="브라우저에서 연어장 찾아줘",
        context={"capabilities": ["browser.search"]},
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.actions[0].type == "open_url"
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_action_compiler_retries_invalid_browser_click_with_validation_error(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "browser.click",
                                    "confidence": 0.95,
                                    "reason": "action request",
                                }
                            )
                        }
                    }
                ]
            }
        if len(calls) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "mode": "direct",
                                    "goal": "Click browser element",
                                    "confidence": 0.9,
                                    "reason": "invalid id",
                                    "actions": [
                                        {
                                            "name": "browser.click",
                                            "args": {"ai_id": 0},
                                            "description": "click element",
                                            "requires_confirm": False,
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct",
                                "goal": "Open first result",
                                "confidence": 0.9,
                                "reason": "fixed validation error",
                                "actions": [
                                    {
                                        "name": "browser.select_result",
                                        "args": {"index": 1},
                                        "description": "open first result",
                                        "requires_confirm": False,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="첫번째 검색 결과를 열어줘",
        context={"capabilities": ["browser.click", "browser.select_result"]},
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.validation_errors == []
    assert decision.actions[0].type == "browser_control"
    assert decision.actions[0].command == "select_result"
    assert decision.actions[0].args == {"index": 1}
    assert len(calls) == 3
    retry_messages = calls[2]["payload"]["messages"]
    retry_payload = json.loads(retry_messages[-1]["content"])
    assert retry_payload["validation_errors"][0]["action_index"] == 0
    assert retry_payload["validation_errors"][0]["action_name"] == "browser.click"
    assert retry_payload["validation_errors"][0]["field"] == "args.ai_id"
    assert retry_payload["validation_errors"][0]["details"] == {"value": 0, "minimum": 1}


def test_action_compiler_retries_no_action_after_positive_intent_gate(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "app.open",
                                    "confidence": 0.95,
                                    "reason": "local app action request",
                                }
                            )
                        }
                    }
                ]
            }
        if len(calls) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "mode": "no_action",
                                    "goal": None,
                                    "confidence": 0.0,
                                    "reason": "mistaken no action",
                                    "actions": [],
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct_sequence",
                                "goal": "Open Sublime Text and type greeting",
                                "confidence": 0.92,
                                "reason": "fixed intent gate contradiction",
                                "actions": [
                                    {
                                        "name": "app.open",
                                        "target": "Sublime Text",
                                        "args": {},
                                        "description": "Open Sublime Text",
                                        "requires_confirm": False,
                                    },
                                    {
                                        "name": "keyboard.type",
                                        "args": {"text": "안녕하세요"},
                                        "description": "Type greeting",
                                        "requires_confirm": False,
                                    },
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="sublimetext켜서 안녕하세요 작성해줘",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "available_applications": [{"name": "Sublime Text", "aliases": ["sublimetext"]}],
        },
    )

    assert decision is not None
    assert decision.should_act is True
    assert [action.type for action in decision.actions] == ["app_control", "keyboard_type"]
    assert len(calls) == 3
    first_plan_payload = json.loads(calls[1]["payload"]["messages"][-1]["content"])
    assert first_plan_payload["intent_gate"]["should_act"] is True
    assert first_plan_payload["intent_gate"]["intent"] == "app.open"
    retry_payload = json.loads(calls[2]["payload"]["messages"][-1]["content"])
    assert retry_payload["validation_errors"][0]["code"] == "intent_gate_contradiction"
    assert retry_payload["validation_errors"][0]["field"] == "mode"
    assert retry_payload["validation_errors"][0]["details"]["intent"] == "app.open"


def test_action_intent_gate_parses_template_key_and_slots() -> None:
    gate = _parse_intent_gate(
        json.dumps(
            {
                "should_act": True,
                "intent": "browser.search+browser.select_result",
                "template_key": "browser_search_open_first",
                "slots": {"query": "네이버웹툰", "open_result_index": 1},
                "confidence": 0.95,
                "reason": "search and open first result",
            }
        )
    )

    assert gate.should_act is True
    assert gate.template_key == "browser_search_open_first"
    assert gate.slots == {"query": "네이버웹툰", "open_result_index": 1}


def test_action_intent_gate_recovers_confidence_from_misplaced_slots() -> None:
    gate = _parse_intent_gate(
        json.dumps(
            {
                "should_act": True,
                "intent": "screen.screenshot",
                "template_key": "screen_screenshot",
                "slots": {"confidence": 0.95},
                "reason": "capture current screen",
            }
        )
    )

    assert gate.should_act is True
    assert gate.confidence == 0.95
    assert gate.template_key == "screen_screenshot"
    assert gate.slots == {}


def test_action_compiler_uses_gate_template_after_repeated_no_action(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "browser.search+browser.select_result",
                                    "template_key": "browser_search_open_first",
                                    "slots": {
                                        "query": "네이버웹툰",
                                        "open_result_index": 1,
                                    },
                                    "confidence": 0.95,
                                    "reason": "search and open first result",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "no_action",
                                "goal": None,
                                "confidence": 0.0,
                                "reason": "compiler failed",
                                "actions": [],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="네이버웹툰 검색해서 들어가 줘",
        context={"capabilities": ["browser.search", "browser.select_result"]},
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct_sequence"
    assert [action.type for action in decision.actions] == ["open_url", "browser_control"]
    assert decision.actions[0].args["query"] == "네이버웹툰"
    assert decision.actions[1].command == "select_result"
    assert decision.actions[1].args == {"index": 1}
    assert len(calls) == 3


def test_action_compiler_repairs_incomplete_multi_step_template_plan(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    intro = "안녕하세요. 저는 JARVIS입니다. 사용자의 작업을 도와주는 AI 어시스턴트입니다."
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "app.open+keyboard.type",
                                    "template_key": "app_open_type",
                                    "slots": {
                                        "app_name": "sublimetext",
                                        "text": intro,
                                    },
                                    "confidence": 0.95,
                                    "reason": "open app, compose requested content, and type it",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct",
                                "goal": "Open Sublime Text",
                                "confidence": 0.8,
                                "reason": "incomplete plan",
                                "actions": [
                                    {
                                        "name": "app.open",
                                        "target": "Sublime Text",
                                        "args": {},
                                        "description": "Open Sublime Text",
                                        "requires_confirm": False,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="sublimetext 켜서 너의 소개 작성해줘",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "available_applications": [
                {"name": "Sublime Text", "aliases": ["sublimetext"]}
            ],
        },
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct_sequence"
    assert [action.type for action in decision.actions] == ["app_control", "keyboard_type"]
    assert decision.actions[0].target == "Sublime Text"
    assert decision.actions[1].payload == intro
    assert len(calls) == 3
    retry_payload = json.loads(calls[2]["payload"]["messages"][-1]["content"])
    retry_error = retry_payload["validation_errors"][0]
    assert retry_error["code"] == "intent_template_incomplete"
    assert retry_error["field"] == "actions"
    assert retry_error["action_name"] == "keyboard.type"
    assert retry_error["details"]["expected_actions"] == ["app.open", "keyboard.type"]
    assert retry_error["details"]["actual_actions"] == ["app.open"]
    assert retry_error["details"]["missing_actions"] == ["keyboard.type"]


def test_action_compiler_does_not_dispatch_incomplete_template_without_slots(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "app.open+keyboard.type",
                                    "template_key": "app_open_type",
                                    "slots": {"app_name": "sublimetext"},
                                    "confidence": 0.95,
                                    "reason": "open app and type requested content",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct",
                                "goal": "Open Sublime Text",
                                "confidence": 0.8,
                                "reason": "incomplete plan",
                                "actions": [
                                    {
                                        "name": "app.open",
                                        "target": "Sublime Text",
                                        "args": {},
                                        "description": "Open Sublime Text",
                                        "requires_confirm": False,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="sublimetext 켜서 소개 작성해줘",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "available_applications": [
                {"name": "Sublime Text", "aliases": ["sublimetext"]}
            ],
        },
    )

    assert decision is not None
    assert decision.should_act is False
    assert decision.execution_mode == "invalid"
    assert decision.actions == []
    assert decision.validation_errors
    assert decision.validation_errors[0].code == "missing_template_slot"
    assert decision.validation_errors[0].field == "slots.text"
    assert len(calls) == 3


def test_action_compiler_uses_working_context_for_followup_template_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "app.open+keyboard.type",
                                    "template_key": "app_open_type",
                                    "slots": {"text": "Hello. I am JARVIS."},
                                    "confidence": 0.95,
                                    "reason": "rewrite previous typed content in English",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "no_action",
                                "goal": None,
                                "confidence": 0.0,
                                "reason": "compiler failed",
                                "actions": [],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="영어로 작성해봐",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "working_context": {
                "active_app": "Sublime Text",
                "last_typed_text": "안녕하세요. 저는 JARVIS입니다.",
            },
            "available_applications": [{"name": "Sublime Text"}],
        },
    )

    assert decision is not None
    assert decision.should_act is True
    assert [action.type for action in decision.actions] == ["app_control", "keyboard_type"]
    assert decision.actions[0].target == "Sublime Text"
    assert decision.actions[1].payload == "Hello. I am JARVIS."
    gate_payload = json.loads(calls[0]["payload"]["messages"][-1]["content"])
    assert gate_payload["runtime_context"]["working_context"]["active_app"] == "Sublime Text"
    compiler_payload = json.loads(calls[1]["payload"]["messages"][-1]["content"])
    assert (
        compiler_payload["runtime_context"]["working_context"]["last_typed_text"]
        == "안녕하세요. 저는 JARVIS입니다."
    )
    assert len(calls) == 2


def test_action_compiler_retries_no_action_when_gate_unavailable_with_working_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            raise TimeoutError("gate timeout")
        if len(calls) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "mode": "no_action",
                                    "goal": None,
                                    "confidence": 0.0,
                                    "reason": "missed contextual follow-up",
                                    "actions": [],
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct_sequence",
                                "goal": "Rewrite previous text in English",
                                "confidence": 0.86,
                                "reason": "contextual follow-up writing request",
                                "actions": [
                                    {
                                        "name": "app.open",
                                        "target": "Sublime Text",
                                        "args": {},
                                        "description": "Open Sublime Text",
                                        "requires_confirm": False,
                                    },
                                    {
                                        "name": "keyboard.type",
                                        "args": {"text": "Hello. I am JARVIS."},
                                        "description": "Type English rewrite",
                                        "requires_confirm": False,
                                    },
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="영어로 작성해봐",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "working_context": {
                "active_app": "Sublime Text",
                "last_typed_text": "안녕하세요. 저는 JARVIS입니다.",
                "recent_actions": [{"action_type": "keyboard_type"}],
            },
        },
    )

    assert decision is not None
    assert decision.should_act is True
    assert [action.type for action in decision.actions] == ["app_control", "keyboard_type"]
    assert decision.actions[1].payload == "Hello. I am JARVIS."
    retry_payload = json.loads(calls[2]["payload"]["messages"][-1]["content"])
    assert retry_payload["validation_errors"][0]["code"] == "working_context_followup_check"
    assert retry_payload["validation_errors"][0]["field"] == "runtime_context.working_context"
    assert retry_payload["validation_errors"][0]["details"]["active_app"] == "Sublime Text"
    assert retry_payload["validation_errors"][0]["details"]["last_typed_text_available"] is True


def test_action_compiler_retries_on_gate_timeout_with_working_context_surface(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            raise TimeoutError("gate timeout")
        if len(calls) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "mode": "no_action",
                                    "goal": None,
                                    "confidence": 0.0,
                                    "reason": "missed contextual follow-up",
                                    "actions": [],
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct",
                                "goal": "Open app",
                                "confidence": 0.86,
                                "reason": "recover contextual follow-up",
                                "actions": [
                                    {
                                        "name": "app.open",
                                        "target": "Sublime Text",
                                        "args": {},
                                        "description": "Open Sublime Text",
                                        "requires_confirm": False,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="영어로 작성해봐",
        context={
            "capabilities": ["app.open"],
            "working_context": {
                "active_app": "Sublime Text",
                "recent_actions": [{"action_type": "app_control"}],
                "last_user_visible_output": "opened",
            },
        },
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct"
    assert decision.actions[0].type == "app_control"
    assert decision.actions[0].target == "Sublime Text"
    retry_payload = json.loads(calls[2]["payload"]["messages"][-1]["content"])
    assert retry_payload["validation_errors"][0]["code"] == "working_context_followup_check"
    assert len(calls) == 3


def test_action_compiler_keeps_recommendation_no_action_with_working_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "should_act": False,
                                "intent": "none",
                                "template_key": None,
                                "slots": {},
                                "confidence": 0.95,
                                "reason": "recommendation answer request",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="점심 메뉴 추천해줘",
        context={
            "working_context": {
                "active_app": "Sublime Text",
                "last_typed_text": "안녕하세요. 저는 JARVIS입니다.",
            }
        },
    )

    assert decision is not None
    assert decision.should_act is False
    assert decision.execution_mode == "no_action"
    assert decision.actions == []
    assert len(calls) == 1


def test_action_compiler_does_not_dispatch_low_confidence_context_followup(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    monkeypatch.setenv("JARVIS_ACTION_INTENT_CONFIDENCE_THRESHOLD", "0.72")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "app.open+keyboard.type",
                                    "template_key": "app_open_type",
                                    "slots": {"text": "Hello."},
                                    "confidence": 0.2,
                                    "reason": "uncertain follow-up",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "no_action",
                                "goal": None,
                                "confidence": 0.0,
                                "reason": "low confidence",
                                "actions": [],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="영어로 작성해봐",
        context={
            "working_context": {
                "active_app": "Sublime Text",
                "last_typed_text": "안녕하세요.",
            }
        },
    )

    assert decision is not None
    assert decision.should_act is False
    assert decision.execution_mode == "no_action"
    assert decision.actions == []
    assert len(calls) == 2


def test_action_compiler_detects_context_text_reuse_from_gate(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "app.open+keyboard.type",
                                    "template_key": "app_open_type",
                                    "slots": {
                                        "app_name": "Sublime Text",
                                        "text": "안녕하세요. 저는 JARVIS입니다.",
                                    },
                                    "confidence": 0.95,
                                    "reason": "rewrite previous typed content in English",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "no_action",
                                "goal": None,
                                "confidence": 0.0,
                                "reason": "compiler failed",
                                "actions": [],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="영어로 작성해봐",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "working_context": {
                "active_app": "Sublime Text",
                "last_typed_text": "안녕하세요. 저는 JARVIS입니다.",
            },
        },
    )

    assert decision is not None
    assert decision.should_act is False
    assert decision.execution_mode == "invalid"
    assert len(decision.validation_errors) == 1
    assert decision.validation_errors[0].code == "working_context_text_reused"
    assert len(calls) == 2


def test_action_compiler_uses_screenshot_template_after_repeated_no_action(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "screen.screenshot",
                                    "template_key": "screen_screenshot",
                                    "slots": {},
                                    "confidence": 0.95,
                                    "reason": "capture current screen",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "no_action",
                                "goal": None,
                                "confidence": 0.0,
                                "reason": "compiler failed",
                                "actions": [],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="현재화면 캡쳐해서 사진으로 띄워줘",
        context={"capabilities": ["screen.screenshot"]},
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct"
    assert len(decision.actions) == 1
    assert decision.actions[0].type == "screenshot"
    assert len(calls) == 3


def test_action_compiler_rejects_gate_template_when_required_slot_missing(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "browser.search+browser.select_result",
                                    "template_key": "browser_search_open_first",
                                    "slots": {"open_result_index": 1},
                                    "confidence": 0.95,
                                    "reason": "search and open first result",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "no_action",
                                "goal": None,
                                "confidence": 0.0,
                                "reason": "compiler failed",
                                "actions": [],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="검색해서 들어가 줘",
        context={"capabilities": ["browser.search", "browser.select_result"]},
    )

    assert decision is not None
    assert decision.should_act is False
    assert decision.execution_mode == "invalid"
    assert decision.actions == []
    assert decision.validation_errors
    assert decision.validation_errors[0].code == "missing_template_slot"
    assert decision.validation_errors[0].field == "slots.query"
    assert len(calls) == 3


def test_action_compiler_adapts_browser_open_without_url_to_browser_action(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "openai_compat")
    calls: list[dict[str, object]] = []

    def fake_post_json(url, payload, *, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "should_act": True,
                                    "intent": "browser.open",
                                    "confidence": 0.95,
                                    "reason": "Korean action request",
                                }
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": "direct",
                                "goal": "Open browser",
                                "confidence": 0.9,
                                "reason": "open configured browser",
                                "actions": [
                                    {
                                        "name": "browser.open",
                                        "args": {},
                                        "description": "Open browser",
                                        "requires_confirm": False,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    decision = ActionCompiler().compile_decision(
        message="브라우저 열어볼래?",
        context={"default_browser": "safari"},
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct"
    assert len(decision.actions) == 1
    assert decision.actions[0].type == "browser"
    assert decision.actions[0].command == "open"
    assert decision.actions[0].args == {"browser": "safari"}


def test_action_plan_parser_normalizes_dict_payloads() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "mode": "direct_sequence",
                "goal": "Open Sublime and type",
                "confidence": 0.9,
                "reason": "local app input",
                "actions": [
                    {
                        "name": "app.open",
                        "target": "Sublime Text",
                        "payload": {},
                        "args": {},
                        "description": "Open Sublime Text",
                    },
                    {
                        "name": "keyboard.type",
                        "payload": {"text": "안녕하세요"},
                        "args": {},
                        "description": "Type greeting",
                    },
                ],
            }
        )
    )

    assert plan.mode == "direct_sequence"
    assert plan.actions[0].payload is None
    assert plan.actions[1].payload == "안녕하세요"
    assert plan.actions[1].args["text"] == "안녕하세요"


def test_action_plan_parser_accepts_structured_action_alias_and_app_name() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "mode": "direct_sequence",
                "goal": "Open app and type",
                "confidence": 0.9,
                "reason": "local app input",
                "actions": [
                    {
                        "action": "app.open",
                        "args": {"app_name": "Sublime Text"},
                        "description": "Open app",
                    },
                    {
                        "action": "keyboard.type",
                        "args": {"text": "안녕하세요"},
                        "description": "Type greeting",
                    },
                ],
            }
        )
    )

    assert plan.mode == "direct_sequence"
    assert plan.actions[0].name == "app.open"
    assert plan.actions[0].target == "Sublime Text"
    assert plan.actions[1].name == "keyboard.type"
    assert plan.actions[1].args["text"] == "안녕하세요"


def test_action_plan_parser_accepts_screen_screenshot() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "mode": "direct",
                "goal": "Capture current screen",
                "confidence": 0.9,
                "reason": "screen capture",
                "actions": [
                    {
                        "name": "screen.screenshot",
                        "args": {},
                        "description": "Capture current screen",
                    }
                ],
            }
        )
    )

    assert plan.mode == "direct"
    assert plan.actions[0].name == "screen.screenshot"


def test_action_intent_gate_accepts_key_value_response(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ACTION_MODEL_PROVIDER", "ollama_chat")

    def fake_post_json(url, payload, *, timeout):
        return {"message": {"role": "assistant", "content": "should_act=true"}}

    monkeypatch.setattr("planner.action_compiler._post_json", fake_post_json)

    gate = ActionCompiler().compile_intent_gate(message="브라우저 열어볼래?")

    assert gate is not None
    assert gate.should_act is True
    assert gate.intent == "action"


def test_action_intent_gate_prompt_guides_korean_polite_requests() -> None:
    prompt = _intent_gate_prompt()

    assert "브라우저 열어볼래?" in prompt
    assert "~해볼래?" in prompt
    assert "~작성해볼래?" in prompt
    assert "sublimetext켜서 안녕하세요 작성해볼래?" in prompt
    assert "app.open+keyboard.type" in prompt
    assert "Do not simplify multi-operation requests" in prompt
    assert "write/create/compose generated content" in prompt
    assert "runtime_context.working_context" in prompt
    assert "영어로 작성해봐" in prompt
    assert "template_key" in prompt
    assert "browser_search_open_first" in prompt
    assert "slots" in prompt
    assert "현재화면 캡쳐해서 사진으로 띄워줘" in prompt
    assert "screen.screenshot" in prompt
    assert "추천해줘" in prompt
    assert "점심 메뉴 추천해줘" in prompt
    assert "recommendation answer request" in prompt
    assert "ordinary conversation" in prompt


def test_action_compiler_prompt_keeps_recommendations_as_no_action() -> None:
    from planner.action_compiler import _system_prompt

    prompt = _system_prompt()

    assert "점심 메뉴 추천해줘" in prompt
    assert "mode no_action" in prompt
    assert "브라우저에서 검색해줘" in prompt
    assert "Fast JSON templates" in prompt
    assert "screen_screenshot" in prompt
    assert "intent_template_incomplete" in prompt
    assert "write/create/compose generated content" in prompt
    assert "working_context.last_typed_text" in prompt
    assert "영어로 작성해봐" in prompt
