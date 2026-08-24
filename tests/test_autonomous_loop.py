import json
from types import SimpleNamespace

from jarvis_contracts import ClientAction, ClientActionEnvelope, ClientActionResult

from planner.autonomous_loop import stream_autonomous_loop


class _CompletedDispatcher:
    context_store = None

    def __init__(self) -> None:
        self.enqueued: list[ClientAction] = []

    def enqueue(self, *, user_id, request_id, action):
        self.enqueued.append(action)
        return ClientActionEnvelope(
            action_id=f"act_{len(self.enqueued)}",
            request_id=request_id,
            action=action,
        )

    def wait_for_result(self, *, action_id, request_id, timeout_seconds=None):
        return ClientActionResult(
            action_id=action_id,
            request_id=request_id,
            status="completed",
            output={"ok": True},
        )


def _step(title: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(title=title, content=content)


def _mouse_click_action() -> ClientAction:
    return ClientAction(
        type="mouse_click",
        command=None,
        args={"x": 1, "y": 1},
        description="click",
        requires_confirm=False,
    )


def _notify_action() -> ClientAction:
    return ClientAction(
        type="notify",
        command=None,
        payload="done",
        args={},
        description="notify",
        requires_confirm=False,
    )


def _parse_events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    events = []
    for chunk in chunks:
        text = chunk.decode("utf-8")
        lines = text.strip().split("\n")
        event_name = lines[0].removeprefix("event: ")
        payload = json.loads(lines[1].removeprefix("data: "))
        events.append((event_name, payload))
    return events


class _CoreClientAlwaysActing:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def deepthink_execute(self, *, request_id, message, plan_steps, user_id, execution_context):
        self.calls.append({"execution_context": list(execution_context)})
        return SimpleNamespace(
            steps=[_step("loop step", "clicked something")],
            actions=[_mouse_click_action()],
        )


class _CoreClientStopsAfterOneRound:
    def __init__(self) -> None:
        self.call_count = 0

    def deepthink_execute(self, *, request_id, message, plan_steps, user_id, execution_context):
        self.call_count += 1
        if self.call_count == 1:
            return SimpleNamespace(
                steps=[_step("round 1", "did the thing")],
                actions=[_mouse_click_action()],
            )
        return SimpleNamespace(steps=[_step("round 2", "nothing left to do")], actions=[])


class _CoreClientReportsDone:
    def deepthink_execute(self, *, request_id, message, plan_steps, user_id, execution_context):
        return SimpleNamespace(
            steps=[_step("done", "goal achieved")],
            actions=[_notify_action()],
        )


def test_stops_when_no_further_actions_returned():
    dispatcher = _CompletedDispatcher()
    core_client = _CoreClientStopsAfterOneRound()

    chunks = list(
        stream_autonomous_loop(
            core_client=core_client,
            action_dispatcher=dispatcher,
            request_id="req1",
            user_id="u1",
            goal="click the button then stop",
            max_iterations=5,
        )
    )

    events = _parse_events(chunks)
    done = [payload for name, payload in events if name == "autonomous_done"][0]
    assert done["stop_reason"] == "no_further_actions"
    assert done["iterations"] == 2
    assert core_client.call_count == 2
    assert len(dispatcher.enqueued) == 1


def test_stops_on_notify_action_as_goal_reported_done():
    dispatcher = _CompletedDispatcher()

    chunks = list(
        stream_autonomous_loop(
            core_client=_CoreClientReportsDone(),
            action_dispatcher=dispatcher,
            request_id="req1",
            user_id="u1",
            goal="tell me when done",
            max_iterations=5,
        )
    )

    events = _parse_events(chunks)
    done = [payload for name, payload in events if name == "autonomous_done"][0]
    assert done["stop_reason"] == "goal_reported_done"
    assert done["iterations"] == 1


def test_stops_at_max_iterations_when_never_signalled_done():
    dispatcher = _CompletedDispatcher()

    chunks = list(
        stream_autonomous_loop(
            core_client=_CoreClientAlwaysActing(),
            action_dispatcher=dispatcher,
            request_id="req1",
            user_id="u1",
            goal="keep playing forever",
            max_iterations=3,
        )
    )

    events = _parse_events(chunks)
    done = [payload for name, payload in events if name == "autonomous_done"][0]
    assert done["stop_reason"] == "max_iterations"
    assert done["iterations"] == 3
    assert len(dispatcher.enqueued) == 3


def test_stops_immediately_when_cancelled():
    dispatcher = _CompletedDispatcher()
    core_client = _CoreClientAlwaysActing()

    chunks = list(
        stream_autonomous_loop(
            core_client=core_client,
            action_dispatcher=dispatcher,
            request_id="req1",
            user_id="u1",
            goal="keep going",
            max_iterations=5,
            is_cancelled=lambda: True,
        )
    )

    events = _parse_events(chunks)
    done = [payload for name, payload in events if name == "autonomous_done"][0]
    assert done["stop_reason"] == "cancelled"
    # cancellation is checked at the top of iteration 1, before any work happens
    assert done["iterations"] == 1
    assert core_client.calls == []


def test_execution_context_grows_across_rounds():
    dispatcher = _CompletedDispatcher()
    core_client = _CoreClientAlwaysActing()

    list(
        stream_autonomous_loop(
            core_client=core_client,
            action_dispatcher=dispatcher,
            request_id="req1",
            user_id="u1",
            goal="observe and act repeatedly",
            max_iterations=3,
        )
    )

    # +1 per round from the step summary, +1 per round from the dispatched
    # action's own context record (stream_action_dispatch_events)
    context_sizes = [len(call["execution_context"]) for call in core_client.calls]
    assert context_sizes == [0, 2, 4]
