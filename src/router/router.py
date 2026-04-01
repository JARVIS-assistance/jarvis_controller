import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from jarvis_contracts.models import ErrorResponse, ExecuteRequest, VerifyRequest
from jarvis_contracts.router_models import (
    ConversationRequest,
    ConversationResponse,
    LoginRequest,
    LoginResponse,
    PlanStepPayload,
    PlanningPayload,
    PrincipalResponse,
)

from ..planner.conversation_orchestrator import orchestrate_conversation_turn
from ..planner.executor import SUPPORTED_ACTIONS, run_execute, run_verify
from ..planner.conversation_routing import ConversationContext

logger = logging.getLogger("jarvis_controller")

api_router = APIRouter()


@api_router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "jarvis-controller"}


@api_router.post("/auth/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request) -> LoginResponse:
    payload = request.app.state.gateway_client.login(
        req.username,
        req.password,
        client_id=request.headers.get("x-client-id"),
        request_id=request.headers.get("x-request-id"),
    )
    return LoginResponse(**payload)


@api_router.post("/auth/logout")
def logout(request: Request) -> dict[str, object]:
    authorization = request.headers.get("authorization", "")
    token = authorization.split(" ", 1)[1]
    return request.app.state.gateway_client.logout(
        token,
        client_id=request.headers.get("x-client-id"),
        request_id=request.headers.get("x-request-id"),
    )


@api_router.get("/auth/me", response_model=PrincipalResponse)
def auth_me(request: Request) -> PrincipalResponse:
    principal = request.state.principal
    return PrincipalResponse(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        role=principal.role,
        active=principal.active,
    )


@api_router.post("/conversation/respond", response_model=ConversationResponse)
def respond(req: ConversationRequest, request: Request) -> ConversationResponse:
    result = orchestrate_conversation_turn(
        req.message,
        core_client=request.app.state.core_client,
        override=req.override.value if req.override else None,
        context=ConversationContext(
            recent_failures=req.recent_failures,
            ambiguity_count=req.ambiguity_count,
            turn_index=req.turn_index,
        ),
    )
    planning = None
    if result.planning_result is not None:
        planning = PlanningPayload(
            goal=result.planning_result.goal,
            constraints=result.planning_result.constraints,
            steps=[
                PlanStepPayload(
                    id=step.id,
                    title=step.title,
                    description=step.description,
                    status=step.status,
                )
                for step in result.planning_result.steps
            ],
            exit_condition=result.planning_result.exit_condition,
            notes=result.planning_result.notes,
        )

    content = result.core_result.content if result.core_result else None
    summary = result.core_result.summary if result.core_result else None
    next_actions = result.core_result.next_actions if result.core_result else []

    return ConversationResponse(
        mode=result.decision.mode,
        triggered=result.decision.triggered,
        confidence=result.decision.confidence,
        reasons=result.decision.reasons,
        handler=result.handler,
        content=content,
        summary=summary,
        next_actions=next_actions,
        planning=planning,
    )


@api_router.post("/execute")
def execute(req: ExecuteRequest, request: Request):
    if req.action not in SUPPORTED_ACTIONS:
        err = ErrorResponse(
            error_code="UNSUPPORTED_ACTION",
            message=f"unsupported action: {req.action}",
            request_id=req.request_id,
            details={"allowed": sorted(SUPPORTED_ACTIONS)},
        )
        logger.error(
            "execute failed request_id=%s reason=unsupported_action", req.request_id
        )
        return JSONResponse(status_code=400, content=err.model_dump())
    result = run_execute(req)
    logger.info("execute success request_id=%s", req.request_id)
    return result


@api_router.post("/verify")
def verify(req: VerifyRequest, request: Request):
    result = run_verify(req)
    logger.info("verify request_id=%s passed=%s", req.request_id, result.passed)
    return result
