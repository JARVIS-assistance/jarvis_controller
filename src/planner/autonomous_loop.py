"""Bounded observe-act loop for open-ended goals ("watch the screen and keep
playing until X happens") that a fixed N-step deepthink plan can't express.

Reuses the existing deepthink execute + action dispatch pipeline unchanged —
this only adds the *repeat* around it. Each round:
  1. Ask jarvis_core's execution model for the next action(s), given the goal
     and everything observed/done so far (execution_context).
  2. Dispatch those actions (server actions — e.g. screen_stream/describe —
     resolve immediately server-side; client actions go over the wire).
  3. Fold the results back into execution_context for the next round.

Stops when: the model returns no further actions (it considers the goal
done), it emits a `notify` action (it's reporting a result to the user),
max_iterations is hit, max_seconds elapses, or the turn is cancelled.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from typing import Any

from jarvis_contracts import ClientAction

from .action_pipeline import sse_event, stream_action_dispatch_events

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_MAX_SECONDS = 180.0

# Emitting one of these action types is treated as the model reporting a
# result to the user, i.e. "I'm done" — matches the existing deepthink
# execution prompt's own convention for search results.
STOP_SIGNAL_ACTION_TYPES = {"notify"}


def stream_autonomous_loop(
    *,
    core_client: Any,
    action_dispatcher: Any,
    request_id: str,
    user_id: str,
    goal: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    is_cancelled: Callable[[], bool] | None = None,
) -> Generator[bytes, None, None]:
    execution_context: list[str] = []
    all_actions: list[ClientAction] = []
    action_results: list[dict[str, object]] = []
    started = time.monotonic()
    stop_reason = "max_iterations"
    iteration = 0

    yield sse_event(
        "autonomous_start",
        {"request_id": request_id, "goal": goal, "max_iterations": max_iterations},
    )

    for iteration in range(1, max_iterations + 1):
        if is_cancelled is not None and is_cancelled():
            stop_reason = "cancelled"
            break
        if time.monotonic() - started > max_seconds:
            stop_reason = "timeout"
            break

        yield sse_event(
            "autonomous_iteration",
            {"request_id": request_id, "iteration": iteration, "goal": goal},
        )

        exec_resp = core_client.deepthink_execute(
            request_id=request_id,
            message=goal,
            plan_steps=[
                {
                    "id": f"loop-{iteration}",
                    "title": goal[:60],
                    "description": goal,
                }
            ],
            user_id=user_id,
            execution_context=execution_context,
        )

        _append_step_context(execution_context, exec_resp.steps)

        if not exec_resp.actions:
            stop_reason = "no_further_actions"
            break

        round_results: list[dict[str, object]] = []
        yield from stream_action_dispatch_events(
            actions=exec_resp.actions,
            request_id=request_id,
            user_id=user_id,
            action_dispatcher=action_dispatcher,
            action_results=round_results,
            all_actions=all_actions,
            execution_context=execution_context,
        )
        action_results.extend(round_results)

        if any(action.type in STOP_SIGNAL_ACTION_TYPES for action in exec_resp.actions):
            stop_reason = "goal_reported_done"
            break

    yield sse_event(
        "autonomous_done",
        {
            "request_id": request_id,
            "iterations": iteration,
            "stop_reason": stop_reason,
            "total_actions": len(all_actions),
        },
    )


def _append_step_context(execution_context: list[str], step_results: Any) -> None:
    for step_result in step_results:
        execution_context.append(f"- {step_result.title}: {step_result.content[:500]}")


def run_autonomous_loop(
    *,
    core_client: Any,
    action_dispatcher: Any,
    request_id: str,
    user_id: str,
    goal: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    is_cancelled: Callable[[], bool] | None = None,
) -> None:
    """Run the same loop as stream_autonomous_loop with nobody reading the SSE
    bytes — for background/detached execution (e.g. a background thread) where
    the HTTP request that started it has already returned. Actions still
    reach the client the same way they always do: dispatched through
    action_dispatcher, delivered by the client's existing action poller.
    """
    for _chunk in stream_autonomous_loop(
        core_client=core_client,
        action_dispatcher=action_dispatcher,
        request_id=request_id,
        user_id=user_id,
        goal=goal,
        max_iterations=max_iterations,
        max_seconds=max_seconds,
        is_cancelled=is_cancelled,
    ):
        pass
