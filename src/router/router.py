import json
import logging
from collections.abc import Generator
from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from jarvis_contracts import (
    ClientAction,
    ClientActionResultRequest,
    ConversationMode as ContractConversationMode,
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

from jarvis_contracts import DeepThinkResponse
from planner.conversation_orchestrator import orchestrate_conversation_turn
from planner.executor import SUPPORTED_ACTIONS, run_execute, run_verify
from planner.conversation_routing import (
    ConversationContext,
    ConversationMode,
    evaluate_conversation_mode,
)
from planner.client_action_intents import infer_client_actions
from planner.dom_link_resolver import resolve_link_from_dom_output
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


def _stream_with_model_logging(
    stream: Generator[bytes, None, None],
    *,
    request_id: str,
    message: str,
) -> Generator[bytes, None, None]:
    event_name: str | None = None
    data_lines: list[str] = []
    logged = False

    for chunk in stream:
        yield chunk
        try:
            line = chunk.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            continue

        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
            data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
            continue
        if line != "" or event_name != "meta" or logged:
            continue

        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            event_name = None
            data_lines = []
            continue

        logger.info(
            "conversation model selected request_id=%s route=%s provider=%s/%s model=%s message=%s",
            request_id,
            payload.get("route"),
            payload.get("provider_mode"),
            payload.get("provider_name"),
            payload.get("model_name"),
            message[:200],
        )
        logged = True
        event_name = None
        data_lines = []


def _format_action_context(
    *,
    action: ClientAction,
    status: str,
    output: dict[str, object],
    error: str | None,
) -> str:
    return (
        f"[클라이언트 실행: {action.type}/{action.command or ''} ({status})]\n"
        f"설명: {action.description}\n"
        f"결과: {json.dumps(output, ensure_ascii=False)}\n"
        f"오류: {error or ''}"
    )


def _fallback_client_actions(
    message: str,
    *,
    request: Request | None = None,
    user_id: str | None = None,
) -> list[ClientAction]:
    context: dict[str, object] = {}
    if request is not None and user_id:
        store = getattr(request.app.state, "action_context", None)
        browser_context = (
            store.browser_context(user_id)
            if store is not None and hasattr(store, "browser_context")
            else None
        )
        if browser_context is not None:
            context = {
                "browser_active": True,
                "last_query": browser_context.last_query,
                "last_url": browser_context.last_url,
            }
    return infer_client_actions(message, context=context or None)


def _action_result_payload(envelope, action_result, action: ClientAction) -> dict[str, object]:
    return {
        "action_id": envelope.action_id,
        "request_id": envelope.request_id,
        "status": action_result.status,
        "output": action_result.output,
        "error": action_result.error,
        "action": action.model_dump(),
    }


def _stream_dispatched_actions(
    *,
    actions: list[ClientAction],
    request_id: str,
    user_id: str,
    action_dispatcher,
    done_content: str,
    done_summary: str,
) -> Generator[bytes, None, None]:
    action_results: list[dict[str, object]] = []
    pending_actions = list(actions)
    while pending_actions:
        action = pending_actions.pop(0)
        envelope = action_dispatcher.enqueue(
            user_id=user_id,
            request_id=request_id,
            action=action,
        )
        yield _sse_event("action_dispatch", envelope.model_dump())
        action_result = action_dispatcher.wait_for_result(
            action_id=envelope.action_id,
            request_id=request_id,
        )
        action_payload = _action_result_payload(envelope, action_result, action)
        action_results.append(action_payload)
        yield _sse_event("action_result", action_payload)
        _record_action_context(
            action_dispatcher=action_dispatcher,
            user_id=user_id,
            action=action,
            status=action_result.status,
            output=action_result.output,
        )
        follow_up = _follow_up_action_from_result(
            action,
            status=action_result.status,
            output=action_result.output,
        )
        if follow_up is not None:
            pending_actions.append(follow_up)

    yield _sse_event(
        "actions",
        {
            "request_id": request_id,
            "total": len(action_results),
            "items": [
                item["action"]
                for item in action_results
                if isinstance(item.get("action"), dict)
            ],
            "results": action_results,
        },
    )


def _record_action_context(
    *,
    action_dispatcher,
    user_id: str,
    action: ClientAction,
    status: str,
    output: dict[str, object],
) -> None:
    store = getattr(action_dispatcher, "context_store", None)
    if store is None:
        return
    store.record_action_result(
        user_id=user_id,
        action=action,
        status=status,
        output=output,
    )
    yield _sse_event(
        "assistant_done",
        {
            "content": done_content,
            "summary": done_summary,
            "has_actions": True,
            "action_count": len(action_results),
            "action_results": action_results,
        },
    )


def _follow_up_action_from_result(
    action: ClientAction,
    *,
    status: str,
    output: dict[str, object],
) -> ClientAction | None:
    if status != "completed":
        return None
    if action.type != "browser_control" or action.command != "extract_dom":
        return None
    if not isinstance(action.args, dict):
        return None
    if action.args.get("purpose") != "resolve_open_request":
        return None
    raw_query = action.args.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        return None
    resolved = resolve_link_from_dom_output(output, query=raw_query)
    if resolved is None:
        return None
    return ClientAction(
        type="open_url",
        command=None,
        target=resolved.href,
        args={"browser": "chrome"},
        description=f"현재 페이지에서 '{raw_query}'에 가장 가까운 링크 열기: {resolved.title or resolved.href}",
        requires_confirm=False,
    )


def _execute_deepthink_steps(
    *,
    core_client,
    action_dispatcher,
    request_id: str,
    message: str,
    plan_steps,
    user_id: str,
) -> tuple[list, str, str, list[ClientAction], list[dict[str, object]]]:
    step_results = []
    all_actions: list[ClientAction] = []
    action_results: list[dict[str, object]] = []
    execution_context: list[str] = []

    for step in plan_steps:
        exec_resp = core_client.deepthink_execute(
            request_id=request_id,
            message=message,
            plan_steps=[
                {"id": step.id, "title": step.title, "description": step.description}
            ],
            user_id=user_id,
            execution_context=execution_context,
        )
        step_results.extend(exec_resp.steps)

        pending_actions = list(exec_resp.actions)
        while pending_actions:
            action = pending_actions.pop(0)
            all_actions.append(action)
            envelope, result = action_dispatcher.dispatch_and_wait(
                user_id=user_id,
                request_id=request_id,
                action=action,
            )
            dumped = _action_result_payload(envelope, result, action)
            action_results.append(dumped)
            execution_context.append(
                _format_action_context(
                    action=action,
                    status=result.status,
                    output=result.output,
                    error=result.error,
                )
            )
            follow_up = _follow_up_action_from_result(
                action,
                status=result.status,
                output=result.output,
            )
            if follow_up is not None:
                pending_actions.append(follow_up)

        for step_result in exec_resp.steps:
            execution_context.append(
                f"- {step_result.title}: {step_result.content[:500]}"
            )

    completed = [s for s in step_results if s.status == "completed"]
    summary = f"{len(completed)}/{len(step_results)} 단계 완료"
    content = "\n\n".join(f"### {s.title}\n{s.content}" for s in step_results)
    return step_results, summary, content, all_actions, action_results


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
    request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"

    yield _sse_event(
        "classification",
        {
            "category": category,
            "mode": decision.mode.value,
            "confidence": decision.confidence,
            "reasons": decision.reasons,
        },
    )

    direct_actions = _fallback_client_actions(
        req.message,
        request=request,
        user_id=principal.user_id,
    )
    if direct_actions and decision.mode != ConversationMode.PLANNING:
        yield _sse_event(
            "thinking",
            {"text": "브라우저 제어 액션을 준비중...", "mode": decision.mode.value},
        )
        yield from _stream_dispatched_actions(
            actions=direct_actions,
            request_id=request_id,
            user_id=principal.user_id,
            action_dispatcher=request.app.state.action_dispatcher,
            done_content="브라우저 제어 액션을 실행 요청했습니다.",
            done_summary="direct client action dispatched",
        )
        return

    # ── realtime: 바로 core에 위임 ─────────────────────────
    if decision.mode == ConversationMode.REALTIME:
        stream = request.app.state.core_client.chat_stream(
            message=req.message,
            task_type="general",
            confirm=False,
            route_override="realtime",
            user_id=principal.user_id,
            user_email=getattr(principal, "email", ""),
            request_id=request_id,
        )
        for chunk in _stream_with_model_logging(
            stream,
            request_id=request_id,
            message=req.message,
        ):
            yield chunk
        return

    # ── deep / planning: 깊은 생각 흐름 ───────────────────
    yield _sse_event(
        "thinking",
        {"text": "조금 더 생각중...", "mode": decision.mode.value},
    )

    # AI 기반 플래닝 (core의 deep model 사용)
    try:
        plan_result = request.app.state.core_client.deepthink_plan(
            request_id=request_id,
            message=req.message,
            user_id=principal.user_id,
        )
    except Exception as exc:
        logger.error("deepthink plan failed, falling back to rule-based: %s", exc)
        # fallback: 규칙 기반 플래닝
        fallback_plan = build_plan(req.message)
        plan_result = type("PlanResult", (), {
            "goal": fallback_plan.goal,
            "steps": [
                type("Step", (), {"id": s.id, "title": s.title, "description": s.description})()
                for s in fallback_plan.steps
            ],
            "constraints": fallback_plan.constraints,
        })()

    # 플랜 요약을 클라이언트에 전송
    plan_summary = "\n".join(
        f"{idx}. {step.title}: {step.description}"
        for idx, step in enumerate(plan_result.steps, start=1)
    )
    yield _sse_event(
        "plan_summary",
        {
            "goal": plan_result.goal,
            "total_steps": len(plan_result.steps),
            "summary": plan_summary,
            "constraints": getattr(plan_result, "constraints", []),
        },
    )

    # planning 모드면 플랜만 전달하고 종료
    if decision.mode == ConversationMode.PLANNING:
        yield _sse_event(
            "assistant_done",
            {"content": f"{plan_result.goal}\n\n{plan_summary}".strip()},
        )
        return

    # 각 단계 진행 상황을 클라이언트에 알림
    for step in plan_result.steps:
        yield _sse_event(
            "plan_step",
            {
                "id": step.id,
                "title": step.title,
                "description": step.description,
                "status": "in_progress",
            },
        )

    # core에 step 단위로 실행 요청하고 결과를 다음 step context로 주입
    try:
        step_results = []
        all_actions = []
        action_results = []
        execution_context: list[str] = []

        for step in plan_result.steps:
            result = request.app.state.core_client.deepthink_execute(
                request_id=request_id,
                message=req.message,
                plan_steps=[
                    {
                        "id": step.id,
                        "title": step.title,
                        "description": step.description,
                    }
                ],
                user_id=principal.user_id,
                execution_context=execution_context,
            )

            for step_result in result.steps:
                step_results.append(step_result)
                yield _sse_event(
                    "plan_step",
                    {
                        "id": step_result.step_id,
                        "title": step_result.title,
                        "description": step_result.content[:200],
                        "status": step_result.status,
                        "actions": [a.model_dump() for a in step_result.actions],
                    },
                )

            pending_actions = list(result.actions)
            while pending_actions:
                action = pending_actions.pop(0)
                all_actions.append(action)
                envelope = request.app.state.action_dispatcher.enqueue(
                    user_id=principal.user_id,
                    request_id=request_id,
                    action=action,
                )
                yield _sse_event(
                    "action_dispatch",
                    envelope.model_dump(),
                )
                action_result = request.app.state.action_dispatcher.wait_for_result(
                    action_id=envelope.action_id,
                    request_id=request_id,
                )
                action_payload = _action_result_payload(envelope, action_result, action)
                action_results.append(action_payload)
                yield _sse_event("action_result", action_payload)
                execution_context.append(
                    _format_action_context(
                        action=action,
                        status=action_result.status,
                        output=action_result.output,
                        error=action_result.error,
                    )
                )
                follow_up = _follow_up_action_from_result(
                    action,
                    status=action_result.status,
                    output=action_result.output,
                )
                if follow_up is not None:
                    pending_actions.append(follow_up)

            for step_result in result.steps:
                execution_context.append(
                    f"- {step_result.title}: {step_result.content[:500]}"
                )

        content = "\n\n".join(
            f"### {s.title}\n{s.content}" for s in step_results
        )
        completed_count = len([s for s in step_results if s.status == "completed"])
        summary = f"{completed_count}/{len(step_results)} 단계 완료"

        if all_actions:
            yield _sse_event(
                "actions",
                {
                    "request_id": request_id,
                    "total": len(all_actions),
                    "items": [a.model_dump() for a in all_actions],
                    "results": action_results,
                },
            )

        yield _sse_event(
            "assistant_done",
            {
                "content": content,
                "summary": summary,
                "has_actions": len(all_actions) > 0,
                "action_count": len(all_actions),
                "action_results": action_results,
            },
        )
    except Exception as exc:
        logger.error("deepthink execute failed: %s", exc)
        fallback_actions = _fallback_client_actions(
            req.message,
            request=request,
            user_id=principal.user_id,
        )
        if fallback_actions:
            yield from _stream_dispatched_actions(
                actions=fallback_actions,
                request_id=request_id,
                user_id=principal.user_id,
                action_dispatcher=request.app.state.action_dispatcher,
                done_content="브라우저 제어 액션을 실행 요청했습니다.",
                done_summary="fallback action dispatched",
            )
            return

        # fallback: deep 모드로 chat_stream 사용
        stream = request.app.state.core_client.chat_stream(
            message=req.message,
            task_type="analysis",
            confirm=False,
            route_override="deep",
            user_id=principal.user_id,
            user_email=getattr(principal, "email", ""),
            request_id=request_id,
        )
        for chunk in _stream_with_model_logging(
            stream,
            request_id=request_id,
            message=req.message,
        ):
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
    core_client = request.app.state.core_client
    request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"

    result = orchestrate_conversation_turn(
        req.message,
        core_client=core_client,
        override=req.override.value if req.override else None,
        context=ConversationContext(
            recent_failures=req.recent_failures,
            ambiguity_count=req.ambiguity_count,
            turn_index=req.turn_index,
        ),
    )

    planning = None
    actions_list: list[dict[str, object]] = []

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

    # deep 모드: AI 플래닝 → 실행 → actions 수집
    if result.decision.mode == ConversationMode.DEEP:
        try:
            principal = request.state.principal
            plan_resp = core_client.deepthink_plan(
                request_id=request_id,
                message=req.message,
                user_id=principal.user_id,
            )
            planning = PlanningPayload(
                goal=plan_resp.goal,
                constraints=plan_resp.constraints,
                steps=[
                    PlanStepPayload(
                        id=s.id, title=s.title, description=s.description, status="pending"
                    )
                    for s in plan_resp.steps
                ],
                exit_condition="all steps executed",
                notes=[],
            )
            (
                _step_results,
                summary,
                content,
                actions,
                _action_results,
            ) = _execute_deepthink_steps(
                core_client=core_client,
                action_dispatcher=request.app.state.action_dispatcher,
                request_id=request_id,
                message=req.message,
                plan_steps=plan_resp.steps,
                user_id=principal.user_id,
            )
            return ConversationResponse(
                mode=result.decision.mode,
                triggered=result.decision.triggered,
                confidence=result.decision.confidence,
                reasons=result.decision.reasons,
                handler="jarvis-core",
                content=content,
                summary=summary,
                next_actions=[],
                planning=planning,
                actions=actions,
            )
        except Exception as exc:
            logger.error("respond deepthink failed, using orchestrator result: %s", exc)

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


def _stream_chat_with_routing(
    req: ChatRequest,
    request: Request,
    principal,
) -> Generator[bytes, None, None]:
    """chat/stream에서도 auto일 때 conversation routing을 적용한다."""
    request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"

    if req.thinking_mode != "auto":
        # 명시적 모드 지정 → core에 바로 위임
        stream = request.app.state.core_client.chat_stream(
            message=req.message,
            task_type=req.task_type,
            confirm=req.confirm,
            route_override=req.thinking_mode,
            user_id=principal.user_id,
            user_email=getattr(principal, "email", ""),
            request_id=request_id,
        )
        yield from _stream_with_model_logging(
            stream,
            request_id=request_id,
            message=req.message,
        )
        return

    # auto 모드: conversation routing으로 분류
    decision = evaluate_conversation_mode(req.message)
    _log_classification(req.message, decision.mode, decision.confidence)

    if decision.mode == ConversationMode.REALTIME:
        stream = request.app.state.core_client.chat_stream(
            message=req.message,
            task_type=req.task_type,
            confirm=req.confirm,
            route_override="realtime",
            user_id=principal.user_id,
            user_email=getattr(principal, "email", ""),
            request_id=request_id,
        )
        yield from _stream_with_model_logging(
            stream,
            request_id=request_id,
            message=req.message,
        )
        return

    # deep / planning → 깊은 생각 흐름을 ConversationRequest로 위임
    conv_req = ConversationRequest(
        message=req.message,
        override=ContractConversationMode(decision.mode.value),
    )
    yield from _stream_orchestrated_conversation(conv_req, request, principal)


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
        _stream_chat_with_routing(req, request, principal),
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


@api_router.get(
    "/client/actions/pending",
    tags=["execution"],
    summary="Fetch pending client actions",
)
def pending_client_actions(
    request: Request,
    limit: int = 20,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.action_dispatcher.pending(
        user_id=principal.user_id,
        limit=max(1, min(limit, 100)),
    )


@api_router.post(
    "/client/actions/{action_id}/result",
    tags=["execution"],
    summary="Submit client action result",
)
def submit_client_action_result(
    action_id: str,
    body: ClientActionResultRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    result = request.app.state.action_dispatcher.complete(
        user_id=principal.user_id,
        action_id=action_id,
        body=body,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="client action not found")
    return result


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
