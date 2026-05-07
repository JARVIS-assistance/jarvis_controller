import time

from jarvis_contracts import ClientAction

from planner.action_context import (
    MAX_TYPED_TEXT_CHARS,
    TRUNCATED_MARKER,
    ActionContextStore,
)


def test_action_context_records_active_app_after_app_open() -> None:
    store = ActionContextStore()

    store.record_action_result(
        user_id="u1",
        action=ClientAction(
            type="app_control",
            command="open",
            target="Sublime Text",
            args={},
            description="Open Sublime Text",
            requires_confirm=False,
        ),
        status="completed",
        output={"message": "opened"},
        action_id="act_app",
    )

    context = store.working_context("u1")
    assert context is not None
    assert context["active_surface"] == "app"
    assert context["active_app"] == "Sublime Text"
    assert context["recent_actions"][0]["action_id"] == "act_app"


def test_action_context_records_keyboard_type_against_active_app() -> None:
    store = ActionContextStore()
    store.record_action_result(
        user_id="u1",
        action=ClientAction(
            type="app_control",
            command="open",
            target="Sublime Text",
            args={},
            description="Open Sublime Text",
            requires_confirm=False,
        ),
        status="completed",
        output={},
        action_id="act_app",
    )

    store.record_action_result(
        user_id="u1",
        action=ClientAction(
            type="keyboard_type",
            command=None,
            target=None,
            payload="안녕하세요. 저는 JARVIS입니다.",
            args={"enter": False},
            description="Type introduction",
            requires_confirm=False,
        ),
        status="completed",
        output={"message": "typed"},
        action_id="act_type",
    )

    context = store.working_context("u1")
    assert context is not None
    assert context["last_typed_text"] == "안녕하세요. 저는 JARVIS입니다."
    assert context["last_typed_target"] == "Sublime Text"
    assert context["recent_actions"][0]["action_type"] == "keyboard_type"
    assert context["recent_actions"][0]["text_summary"] == "안녕하세요. 저는 JARVIS입니다."


def test_action_context_ignores_duplicate_action_id() -> None:
    store = ActionContextStore()
    action = ClientAction(
        type="keyboard_type",
        command=None,
        target=None,
        payload="first",
        args={},
        description="Type first",
        requires_confirm=False,
    )

    store.record_action_result(
        user_id="u1",
        action=action,
        status="completed",
        output={},
        action_id="act_dup",
    )
    store.record_action_result(
        user_id="u1",
        action=action.model_copy(update={"payload": "second"}),
        status="completed",
        output={},
        action_id="act_dup",
    )

    context = store.working_context("u1")
    assert context is not None
    assert context["last_typed_text"] == "first"
    assert len(context["recent_actions"]) == 1


def test_action_context_expires_working_context_after_ttl() -> None:
    store = ActionContextStore(ttl_seconds=0.001)
    store.record_action_result(
        user_id="u1",
        action=ClientAction(
            type="app_control",
            command="open",
            target="Sublime Text",
            args={},
            description="Open Sublime Text",
            requires_confirm=False,
        ),
        status="completed",
        output={},
        action_id="act_app",
    )

    time.sleep(0.01)

    assert store.working_context("u1") is None


def test_action_context_truncates_long_typed_text() -> None:
    store = ActionContextStore()
    long_text = "a" * (MAX_TYPED_TEXT_CHARS + 100)

    store.record_action_result(
        user_id="u1",
        action=ClientAction(
            type="keyboard_type",
            command=None,
            target=None,
            payload=long_text,
            args={},
            description="Type long text",
            requires_confirm=False,
        ),
        status="completed",
        output={},
        action_id="act_type",
    )

    context = store.working_context("u1")
    assert context is not None
    assert len(context["last_typed_text"]) <= MAX_TYPED_TEXT_CHARS
    assert context["last_typed_text"].endswith(TRUNCATED_MARKER)
