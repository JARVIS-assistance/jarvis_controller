import json

from jarvis_contracts import ClientAction, ClientActionEnvelope, ClientActionResult

from planner.action_pipeline import action_result_timeout_seconds, stream_action_dispatch_events


def test_confirm_likely_actions_use_longer_result_timeout(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CLIENT_ACTION_CONFIRM_TIMEOUT_SECONDS", "37")

    action = ClientAction(
        type="keyboard_type",
        payload="안녕하세요",
        args={},
        description="Type greeting",
        requires_confirm=False,
    )

    assert action_result_timeout_seconds(action) == 37.0


def test_non_confirm_actions_use_dispatcher_default_timeout() -> None:
    action = ClientAction(
        type="browser",
        command="open",
        args={},
        description="Open browser",
        requires_confirm=False,
    )

    assert action_result_timeout_seconds(action) is None


def _collect_events(chunks: list[bytes]) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    current_event: str | None = None
    data_lines: list[str] = []

    for chunk in chunks:
        text = chunk.decode("utf-8")
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("event:"):
                current_event = line[len("event:") :].strip()
                data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
                continue
            if not line and current_event:
                payload = json.loads("\n".join(data_lines) or "{}")
                events.append((current_event, payload))
                current_event = None
                data_lines = []
    if current_event and data_lines:
        events.append((current_event, json.loads("\n".join(data_lines) or "{}")))
    return events


def test_stream_action_dispatch_events_emits_plan_steps() -> None:
    action = ClientAction(
        type="open_url",
        target="https://example.com",
        args={},
        description="open example",
        requires_confirm=False,
    )

    class Dispatcher:
        def enqueue(self, *, user_id, request_id, action):
            assert user_id == "u1"
            assert request_id == "req_stream"
            assert action is action
            return ClientActionEnvelope(
                action_id="act_plan",
                request_id=request_id,
                action=action,
            )

        def wait_for_result(self, *, action_id, request_id, timeout_seconds=None):
            assert action_id == "act_plan"
            assert request_id == "req_stream"
            return ClientActionResult(
                action_id=action_id,
                request_id=request_id,
                status="completed",
                output={"ok": True},
            )

        context_store = None

    chunks = list(
        stream_action_dispatch_events(
            actions=[action],
            request_id="req_stream",
            user_id="u1",
            action_dispatcher=Dispatcher(),
        )
    )
    events = _collect_events(chunks)
    statuses = [event for event, _ in events]

    assert statuses[0] == "plan_step"
    assert statuses[1] == "action_dispatch"
    assert statuses[2] == "plan_step"
    assert statuses[3] == "plan_step"
    assert statuses[4] == "action_result"
    assert events[0][1]["id"] == "act_plan"
    assert events[0][1]["status"] == "queued"
    assert events[2][1]["status"] == "in_progress"
    assert events[4][1]["status"] == "completed"


def test_stream_action_dispatch_events_marks_failed_plan_step() -> None:
    action = ClientAction(
        type="browser_control",
        command="click_element",
        target="active_tab",
        args={},
        description="click result",
        requires_confirm=False,
    )

    class Dispatcher:
        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_plan_fail",
                request_id=request_id,
                action=action,
            )

        def wait_for_result(self, *, action_id, request_id, timeout_seconds=None):
            return ClientActionResult(
                action_id=action_id,
                request_id=request_id,
                status="failed",
                output={},
                error="boom",
            )

        context_store = None

    events = _collect_events(
        list(
            stream_action_dispatch_events(
                actions=[action],
                request_id="req_stream_fail",
                user_id="u1",
                action_dispatcher=Dispatcher(),
            )
        )
    )
    terminal_status = [
        payload["status"]
        for name, payload in events
        if name == "plan_step"
        and payload["status"] in {"failed", "timeout", "rejected", "invalid", "completed"}
    ]
    assert terminal_status == ["failed"]
