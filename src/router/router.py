import json
import logging
from collections.abc import Generator
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from jarvis_contracts import (
    ConversationRequest,
    ConversationResponse,
    ErrorResponse,
    ExecuteRequest,
    LoginRequest,
    LoginResponse,
    PlanStepPayload,
    PlanningPayload,
    PrincipalResponse,
    VerifyRequest,
)

from planner.conversation_orchestrator import orchestrate_conversation_turn
from planner.executor import SUPPORTED_ACTIONS, run_execute, run_verify
from planner.conversation_routing import (
    ConversationContext,
    ConversationMode,
    evaluate_conversation_mode,
)
from planner.planning_engine import build_plan

logger = logging.getLogger("jarvis_controller")

api_router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
TokenAuth = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(bearer_scheme),
]
AuthHeaderDoc = Annotated[
    str | None,
    Header(
        alias="Authorization",
        description="Bearer access token. Example: `Bearer eyJ...`",
    ),
]


# ── chat request/response (controller용) ────────────────────


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    task_type: Literal["general", "analysis", "execution"] = "general"
    confirm: bool = False
    thinking_mode: Literal["auto", "realtime", "deep"] = "auto"


class ChatResponse(BaseModel):
    request_id: str
    route: str
    provider_mode: str
    provider_name: str
    model_name: str
    content: str


class ModelConfigRequest(BaseModel):
    provider_mode: Literal["token", "local"]
    provider_name: str = Field(min_length=1, max_length=60)
    model_name: str = Field(min_length=1, max_length=120)
    api_key: Optional[str] = None
    endpoint: Optional[str] = None
    is_default: bool = False
    supports_stream: bool = True
    supports_realtime: bool = False
    transport: Literal["http_sse", "websocket"] = "http_sse"
    input_modalities: str = "text"
    output_modalities: str = "text"


class ModelSelectionRequest(BaseModel):
    realtime_model_config_id: str | None = None
    deep_model_config_id: str | None = None


class PersonaRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    prompt_template: str = Field(min_length=1)
    tone: str | None = Field(default=None, max_length=40)
    alias: str | None = Field(default=None, max_length=80)


class PersonaSelectionRequest(BaseModel):
    user_persona_id: str = Field(min_length=1)


class MemoryRequest(BaseModel):
    type: Literal["preference", "fact", "task"]
    content: str = Field(min_length=1)
    importance: int = Field(default=3, ge=1, le=5)
    chat_id: str | None = None
    source_message_id: str | None = None
    expires_at: str | None = None


class SignupRequest(BaseModel):
    email: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    password: str = Field(min_length=1)


class SignupResponse(BaseModel):
    access_token: str
    user_id: str
    email: str
    name: str | None = None


def _sse_event(event: str, payload: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode(
        "utf-8"
    )


def _log_classification(message: str, mode: ConversationMode, confidence: float) -> None:
    category = "general" if mode == ConversationMode.REALTIME else "deep"
    logger.info(
        "conversation classified category=%s mode=%s confidence=%.2f message=%s",
        category,
        mode.value,
        confidence,
        message[:200],
    )


def _stream_orchestrated_conversation(
    req: ConversationRequest,
    request: Request,
    principal,
) -> Generator[bytes, None, None]:
    decision = evaluate_conversation_mode(
        req.message,
        override=req.override.value if req.override else None,
        context=ConversationContext(
            recent_failures=req.recent_failures,
            ambiguity_count=req.ambiguity_count,
            turn_index=req.turn_index,
        ),
    )
    _log_classification(req.message, decision.mode, decision.confidence)

    category = "general" if decision.mode == ConversationMode.REALTIME else "deep"
    yield _sse_event(
        "classification",
        {
            "category": category,
            "mode": decision.mode.value,
            "confidence": decision.confidence,
            "reasons": decision.reasons,
        },
    )

    if decision.mode != ConversationMode.REALTIME:
        yield _sse_event(
            "thinking",
            {
                "text": "DeepThinking...",
                "mode": decision.mode.value,
            },
        )
        plan = build_plan(req.message)
        for step in plan.steps:
            logger.info("deep-thinking plan step title=%s", step.title)
            yield _sse_event(
                "plan_step",
                {
                    "id": step.id,
                    "title": step.title,
                    "description": step.description,
                    "status": "in_progress",
                },
            )

    if decision.mode == ConversationMode.PLANNING:
        plan = build_plan(req.message)
        summary = "\n".join(f"{index}. {step.description}" for index, step in enumerate(plan.steps, start=1))
        yield _sse_event(
            "assistant_done",
            {
                "content": f"{plan.goal}\n\n{summary}".strip(),
            },
        )
        return

    route_override = "deep" if decision.mode == ConversationMode.DEEP else "realtime"
    stream = request.app.state.core_client.chat_stream(
        message=req.message,
        task_type="analysis" if decision.mode == ConversationMode.DEEP else "general",
        confirm=False,
        route_override=route_override,
        user_id=principal.user_id,
        user_email=getattr(principal, "email", ""),
        request_id=request.headers.get("x-request-id", ""),
    )
    for chunk in stream:
        yield chunk


# ── health ──────────────────────────────────────────────────


@api_router.get("/health", tags=["health"], summary="Health check")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "jarvis-controller"}


# ── auth ────────────────────────────────────────────────────


@api_router.post(
    "/auth/login",
    response_model=LoginResponse,
    tags=["auth"],
    summary="Login",
)
def login(req: LoginRequest, request: Request) -> LoginResponse:
    payload = request.app.state.gateway_client.login(
        req.username,
        req.password,
        client_id=request.headers.get("x-client-id"),
        request_id=request.headers.get("x-request-id"),
    )
    return LoginResponse(**payload)


@api_router.post(
    "/auth/signup",
    response_model=SignupResponse,
    tags=["auth"],
    summary="Signup",
)
def signup(
    req: SignupRequest,
    request: Request,
) -> SignupResponse:
    payload = request.app.state.gateway_client.signup(
        email=req.email,
        name=req.name,
        password=req.password,
        client_id=request.headers.get("x-client-id"),
        request_id=request.headers.get("x-request-id"),
    )
    return SignupResponse(**payload)


@api_router.post("/auth/logout", tags=["auth"], summary="Logout")
def logout(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> dict[str, object]:
    _ = authorization_header
    authorization = request.headers.get("authorization", "")
    token = authorization.split(" ", 1)[1]
    return request.app.state.gateway_client.logout(
        token,
        client_id=request.headers.get("x-client-id"),
        request_id=request.headers.get("x-request-id"),
    )


@api_router.get(
    "/auth/me", response_model=PrincipalResponse, tags=["auth"], summary="Current user"
)
def auth_me(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> PrincipalResponse:
    _ = authorization_header
    principal = request.state.principal
    return PrincipalResponse(
        user_id=principal.user_id,
        active=principal.active,
    )


# ── conversation (orchestration) ────────────────────────────


@api_router.post(
    "/conversation/respond",
    response_model=ConversationResponse,
    tags=["conversation"],
    summary="Get orchestrated conversation response",
)
def respond(
    req: ConversationRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> ConversationResponse:
    _ = authorization_header
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


@api_router.post(
    "/conversation/stream",
    tags=["conversation"],
    summary="Stream orchestrated conversation response",
)
def conversation_stream(
    req: ConversationRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> StreamingResponse:
    _ = authorization_header
    principal = request.state.principal
    return StreamingResponse(
        _stream_orchestrated_conversation(req, request, principal),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── chat ────────────────────────────────────────────────────


@api_router.post(
    "/chat/request", response_model=ChatResponse, tags=["chat"], summary="Request chat"
)
def chat_request(
    req: ChatRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> ChatResponse:
    _ = authorization_header
    principal = request.state.principal
    result = request.app.state.core_client.chat_request(
        message=req.message,
        task_type=req.task_type,
        confirm=req.confirm,
        route_override=None if req.thinking_mode == "auto" else req.thinking_mode,
        user_id=principal.user_id,
        user_email=getattr(principal, "email", ""),
        request_id=request.headers.get("x-request-id", ""),
    )
    return ChatResponse(**result)


@api_router.post("/chat/stream", tags=["chat"], summary="Stream chat response")
def chat_stream(
    req: ChatRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> StreamingResponse:
    _ = authorization_header
    principal = request.state.principal
    return StreamingResponse(
        request.app.state.core_client.chat_stream(
            message=req.message,
            task_type=req.task_type,
            confirm=req.confirm,
            route_override=None if req.thinking_mode == "auto" else req.thinking_mode,
            user_id=principal.user_id,
            user_email=getattr(principal, "email", ""),
            request_id=request.headers.get("x-request-id", ""),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── model config ────────────────────────────────────────────


@api_router.post("/chat/model-config", tags=["chat"], summary="Create model config")
def create_model_config(
    req: ModelConfigRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.create_model_config(
        user_id=principal.user_id,
        body=req.model_dump(),
    )


@api_router.get("/chat/model-config", tags=["chat"], summary="List model configs")
def list_model_configs(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.list_model_configs(
        user_id=principal.user_id,
    )


@api_router.put("/chat/model-config/{model_config_id}", tags=["chat"], summary="Update model config")
def update_model_config(
    model_config_id: str,
    req: ModelConfigRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.update_model_config(
        user_id=principal.user_id,
        model_config_id=model_config_id,
        body=req.model_dump(),
    )


@api_router.post("/chat/model-selection", tags=["chat"], summary="Set model selection")
def set_model_selection(
    req: ModelSelectionRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.set_model_selection(
        user_id=principal.user_id,
        body=req.model_dump(),
    )


@api_router.get("/chat/model-selection", tags=["chat"], summary="Get model selection")
def get_model_selection(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.get_model_selection(
        user_id=principal.user_id,
    )


@api_router.post("/chat/persona", tags=["chat"], summary="Create persona")
def create_persona(
    req: PersonaRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.create_persona(
        user_id=principal.user_id,
        body=req.model_dump(),
    )


@api_router.get("/chat/persona", tags=["chat"], summary="List personas")
def list_personas(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.list_personas(user_id=principal.user_id)


@api_router.put("/chat/persona/{user_persona_id}", tags=["chat"], summary="Update persona")
def update_persona(
    user_persona_id: str,
    req: PersonaRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.update_persona(
        user_id=principal.user_id,
        user_persona_id=user_persona_id,
        body=req.model_dump(),
    )


@api_router.post("/chat/persona/select", tags=["chat"], summary="Select active persona")
def select_persona(
    req: PersonaSelectionRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.select_persona(
        user_id=principal.user_id,
        body=req.model_dump(),
    )


@api_router.post("/chat/memory", tags=["chat"], summary="Create memory item")
def create_memory(
    req: MemoryRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.create_memory(
        user_id=principal.user_id,
        body=req.model_dump(),
    )


@api_router.get("/chat/memory", tags=["chat"], summary="List memory items")
def list_memory(
    request: Request,
    chat_id: str | None = None,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.list_memory(
        user_id=principal.user_id,
        chat_id=chat_id,
    )


# ── execute / verify ────────────────────────────────────────


@api_router.post("/execute", tags=["execution"], summary="Execute action")
def execute(
    req: ExecuteRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
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


@api_router.post("/verify", tags=["execution"], summary="Verify result")
def verify(
    req: VerifyRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    result = run_verify(req)
    logger.info("verify request_id=%s passed=%s", req.request_id, result.passed)
    return result
