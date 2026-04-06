from __future__ import annotations

from dataclasses import dataclass

from middleware.core_client import CoreClient, CoreResponse
from planner.conversation_routing import (
    ConversationContext,
    ConversationMode,
    RoutingDecision,
    evaluate_conversation_mode,
)
from planner.planning_engine import PlanningResult, build_plan


@dataclass(slots=True)
class OrchestrationResult:
    decision: RoutingDecision
    planning_result: PlanningResult | None = None
    core_result: CoreResponse | None = None
    handler: str = "jarvis-controller"


def orchestrate_conversation_turn(
    message: str,
    *,
    core_client: CoreClient,
    override: str | None = None,
    context: ConversationContext | None = None,
) -> OrchestrationResult:
    decision = evaluate_conversation_mode(message, override=override, context=context)

    if decision.mode == ConversationMode.PLANNING:
        return OrchestrationResult(
            decision=decision,
            planning_result=build_plan(message),
            handler="jarvis-controller",
        )

    if decision.mode == ConversationMode.DEEP:
        return OrchestrationResult(
            decision=decision,
            core_result=core_client.run_deep_thinking(message),
            handler="jarvis-core",
        )

    return OrchestrationResult(
        decision=decision,
        core_result=core_client.run_realtime_conversation(message),
        handler="jarvis-core",
    )
