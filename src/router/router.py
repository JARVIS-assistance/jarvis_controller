import concurrent.futures
import json
import logging
import os
from collections.abc import Generator
from typing import Annotated, Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jarvis_contracts import (
    ClientAction,
    ClientActionResultRequest,
    ConversationRequest,
    ConversationResponse,
    ErrorResponse,
    ExecuteRequest,
    LoginRequest,
    LoginResponse,
    PlanningPayload,
    PlanStepPayload,
    PrincipalResponse,
    VerifyRequest,
    action_registry_payload,
)
from jarvis_contracts import (
    ConversationMode as ContractConversationMode,
)
from pydantic import BaseModel, Field

from planner.action_intent_classifier import (
    DIRECT_EXECUTION_MODES,
    ActionIntentDecision,
    action_compiler_prompt_payload,
    classify_client_action_intent_decision,
    compile_action_decision_from_model_text,
    parse_embedded_actions_from_text,
    should_try_client_action_classifier,
)
from planner.action_pipeline import (
    action_completion_message as _action_completion_message,
)
from planner.action_pipeline import (
    action_result_payload as _action_result_payload,
)
from planner.action_pipeline import (
    dispatch_actions_sync as _dispatch_actions_sync,
)
from planner.action_pipeline import (
    follow_up_action_from_result as _follow_up_action_from_result,
)
from planner.action_pipeline import (
    format_action_context as _format_action_context,
)
from planner.action_pipeline import (
    stream_dispatched_actions as _stream_dispatched_actions,
)
from planner.conversation_orchestrator import orchestrate_conversation_turn
from planner.conversation_routing import (
    ConversationContext,
    ConversationMode,
    RoutingDecision,
    evaluate_conversation_mode,
)
from planner.executor import SUPPORTED_ACTIONS, run_execute, run_verify
from planner.planning_engine import build_plan

logger = logging.getLogger("jarvis_controller")

api_router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
_ACTION_ARBITRATION_BUFFER_SECONDS = "JARVIS_ACTION_ARBITRATION_BUFFER_SECONDS"
_ACTION_ARBITRATION_DEFAULT_SECONDS = 0.0
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


class RuntimeApplicationRequest(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=160)
    display_name: str | None = Field(default=None, max_length=160)
    aliases: list[str] = Field(default_factory=list)
    bundle_id: str | None = Field(default=None, max_length=240)
    path: str | None = None
    executable: str | None = Field(default=None, max_length=240)
    kind: str | None = Field(default=None, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TerminalProfileRequest(BaseModel):
    enabled: bool = False
    shell: str | None = Field(default=None, max_length=80)
    shell_path: str | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    supports_pty: bool = False
    requires_confirm: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class RuntimeProfileRequest(BaseModel):
    platform: str | None = Field(default=None, max_length=40)
    default_browser: str | None = Field(default=None, max_length=80)
    capabilities: list[str] = Field(default_factory=list)
    applications: list[RuntimeApplicationRequest] = Field(default_factory=list)
    terminal: TerminalProfileRequest | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue

        for line in text.splitlines():
            line = line.rstrip("\r\n")
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
                "conversation model selected request_id=%s route=%s provider=%s/%s "
                "model=%s message=%s",
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


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _action_arbitration_buffer_seconds() -> float:
    return max(0.0, _float_env(
        _ACTION_ARBITRATION_BUFFER_SECONDS,
        _ACTION_ARBITRATION_DEFAULT_SECONDS,
    ))


def _resolve_client_action_decision(
    message: str,
    *,
    request: Request | None,
    user_id: str | None,
) -> ActionIntentDecision | None:
    return _client_action_decision(message, request=request, user_id=user_id)


def _start_action_decision_future(
    message: str,
    *,
    request: Request,
    user_id: str,
) -> tuple[
    concurrent.futures.ThreadPoolExecutor,
    concurrent.futures.Future[ActionIntentDecision | None],
]:
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="action-intent",
    )
    return executor, executor.submit(
        _resolve_client_action_decision,
        message,
        request=request,
        user_id=user_id,
    )


def _start_routing_decision_future(
    message: str,
    *,
    override: str | None = None,
    context: ConversationContext | None = None,
) -> tuple[
    concurrent.futures.ThreadPoolExecutor,
    concurrent.futures.Future[RoutingDecision],
]:
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="conversation-routing",
    )
    return executor, executor.submit(
        evaluate_conversation_mode,
        message,
        override=override,
        context=context,
    )


def _classification_chunks_for_decision(decision: RoutingDecision) -> list[bytes]:
    category = "general" if decision.mode == ConversationMode.REALTIME else "deep"
    return [
        _sse_event(
            "classification",
            {
                "category": category,
                "mode": decision.mode.value,
                "confidence": decision.confidence,
                "reasons": decision.reasons,
            },
        )
    ]


def _ready_routing_chunks(
    route_future: concurrent.futures.Future[RoutingDecision] | None,
) -> list[bytes]:
    if route_future is None or not route_future.done():
        return []
    try:
        decision = route_future.result()
    except Exception as exc:
        logger.warning("conversation routing decision failed after stream start: %s", exc)
        return []
    _log_classification("", decision.mode, decision.confidence)
    return _classification_chunks_for_decision(decision)


def _stream_realtime_with_action_arbitration(
    stream: Generator[bytes, None, None],
    *,
    action_future: concurrent.futures.Future[ActionIntentDecision | None] | None,
    request_id: str,
    message: str,
    user_id: str,
    action_dispatcher,
    context: dict[str, object] | None,
) -> Generator[bytes, None, None]:
    """Stream core bytes immediately while action classification runs in parallel."""
    emitted_action_intent = False

    def ready_decision_chunks() -> tuple[list[bytes], bool]:
        nonlocal emitted_action_intent
        if action_future is None or emitted_action_intent or not action_future.done():
            return [], False
        try:
            decision = action_future.result()
        except Exception as exc:
            logger.warning("action intent decision failed after stream start: %s", exc)
            decision = None

        emitted_action_intent = True
        chunks = [_sse_event(
            "action_intent",
            _action_intent_payload(decision, unavailable=decision is None),
        )]
        if _is_direct_action_decision(decision):
            chunks.extend(
                _stream_direct_action_decision(
                    decision=decision,
                    message=message,
                    request_id=request_id,
                    user_id=user_id,
                    action_dispatcher=action_dispatcher,
                )
            )
            return chunks, False
        return chunks, False

    try:
        for chunk in _stream_with_embedded_action_intercept(
            stream,
            request_id=request_id,
            message=message,
            user_id=user_id,
            action_dispatcher=action_dispatcher,
            context=context,
        ):
            yield chunk
            decision_chunks, stopped = ready_decision_chunks()
            for decision_chunk in decision_chunks:
                yield decision_chunk
            if stopped:
                return

        decision_chunks, stopped = ready_decision_chunks()
        for decision_chunk in decision_chunks:
            yield decision_chunk
        if stopped:
            return
    except Exception:
        if action_future is not None:
            action_future.cancel()
        raise


def _stream_realtime_with_parallel_decisions(
    stream: Generator[bytes, None, None],
    *,
    action_future: concurrent.futures.Future[ActionIntentDecision | None] | None,
    route_future: concurrent.futures.Future[RoutingDecision] | None,
    request_id: str,
    message: str,
    user_id: str,
    action_dispatcher,
    context: dict[str, object] | None,
) -> Generator[bytes, None, None]:
    """Proxy realtime bytes first, then surface routing/action decisions if ready."""
    emitted_routing = False
    for chunk in _stream_realtime_with_action_arbitration(
        stream,
        action_future=action_future,
        request_id=request_id,
        message=message,
        user_id=user_id,
        action_dispatcher=action_dispatcher,
        context=context,
    ):
        yield chunk
        if not emitted_routing:
            route_chunks = _ready_routing_chunks(route_future)
            if route_chunks:
                emitted_routing = True
                for route_chunk in route_chunks:
                    yield route_chunk

    if not emitted_routing:
        for route_chunk in _ready_routing_chunks(route_future):
            yield route_chunk



def _stream_with_embedded_action_intercept(
    stream: Generator[bytes, None, None],
    *,
    request_id: str,
    message: str,
    user_id: str,
    action_dispatcher,
    context: dict[str, object] | None,
) -> Generator[bytes, None, None]:
    """Pass realtime chunks through while converting embedded action blocks when found."""
    event_name: str | None = None
    data_lines: list[str] = []
    logged = False
    embedded_actions: list[ClientAction] = []
    embedded_validation_errors = []
    saw_action_block = False

    for chunk in stream:
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            yield chunk
            continue

        suppress_current_chunk = False
        for line in text.splitlines():
            line = line.rstrip("\r\n")
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
                continue
            if line != "":
                continue

            if event_name is None:
                data_lines = []
                continue
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                event_name = None
                data_lines = []
                continue

            if event_name == "meta" and not logged:
                logger.info(
                    "conversation model selected request_id=%s route=%s "
                    "provider=%s/%s model=%s message=%s",
                    request_id,
                    payload.get("route"),
                    payload.get("provider_mode"),
                    payload.get("provider_name"),
                    payload.get("model_name"),
                    message[:200],
                )
                logged = True
            elif event_name in {"assistant_done", "conversation.done", "done"}:
                content = payload.get("content") or payload.get("text")
                if isinstance(content, str):
                    saw_action_block = _contains_action_block(content)
                    embedded_result = parse_embedded_actions_from_text(
                        content,
                        context=context,
                    )
                    embedded_actions = embedded_result.actions
                    embedded_validation_errors = embedded_result.issues
                    saw_action_block = saw_action_block or embedded_result.saw_action_block
                    suppress_current_chunk = saw_action_block

            event_name = None
            data_lines = []

        if suppress_current_chunk:
            break
        yield chunk

    if embedded_actions:
        logger.warning(
            "embedded assistant action block converted to queued client actions "
            "request_id=%s actions=%d message=%s",
            request_id,
            len(embedded_actions),
            message[:200],
        )
        yield from _stream_dispatched_actions(
            actions=embedded_actions,
            request_id=request_id,
            user_id=user_id,
            action_dispatcher=action_dispatcher,
            done_content="요청한 작업을 실행했습니다.",
            done_summary="embedded assistant action converted to dispatch",
        )
        return

    if saw_action_block:
        yield _sse_event(
            "action_compile_retry",
            {
                "request_id": request_id,
                "status": "retrying_compile",
                "reason": "embedded assistant action was not directly dispatchable",
                "validation_errors": [
                    issue.model_dump()
                    for issue in embedded_validation_errors
                    if hasattr(issue, "model_dump")
                ],
            },
        )
        retry_decision = classify_client_action_intent_decision(
            message,
            context=context,
            validation_errors=embedded_validation_errors,
        )
        if _is_direct_action_decision(retry_decision):
            logger.warning(
                "embedded assistant action block was invalid; action classifier "
                "retry produced dispatchable actions request_id=%s actions=%d "
                "message=%s",
                request_id,
                len(retry_decision.actions),
                message[:200],
            )
            yield from _stream_dispatched_actions(
                actions=retry_decision.actions,
                request_id=request_id,
                user_id=user_id,
                action_dispatcher=action_dispatcher,
                done_content="요청한 작업을 실행했습니다.",
                done_summary="embedded action recovered by action classifier",
            )
            return

        logger.warning(
            "embedded assistant action block suppressed because it was not "
            "dispatchable request_id=%s message=%s raw=%s",
            request_id,
            message[:200],
            _redact_log_text(content)[:1000] if isinstance(content, str) else "",
        )
        error_text = _embedded_suppressed_error(embedded_validation_errors, retry_decision)
        yield _sse_event(
            "assistant_done",
            {
                "content": (
                    f"실행할 액션을 큐에 넣지 못해 실행하지 않았습니다. {error_text}"
                ).strip(),
                "summary": "embedded assistant action suppressed",
                "has_actions": False,
                "action_count": 0,
                "action_results": [],
                "error": error_text,
                "validation_errors": [
                    issue.model_dump()
                    for issue in embedded_validation_errors
                    if hasattr(issue, "model_dump")
                ],
            },
        )
        return

    return


def _redact_log_text(value: str) -> str:
    return value.replace("\n", "\\n").replace("\r", "\\r")


def _contains_action_block(content: str) -> bool:
    lowered = content.lower()
    return (
        "```actions" in lowered
        or (
            "```json" in lowered
            and '"type"' in lowered
            and (
                '"app_control"' in lowered
                or '"browser_control"' in lowered
                or '"open_url"' in lowered
                or '"terminal"' in lowered
                or '"keyboard_type"' in lowered
                or '"calendar_control"' in lowered
            )
        )
    )


def _embedded_suppressed_error(
    validation_errors: list[object],
    retry_decision: ActionIntentDecision | None,
) -> str:
    if retry_decision is None:
        return "액션 컴파일러가 사용할 수 없거나 유효한 액션을 만들지 못했습니다."
    if getattr(retry_decision, "validation_errors", None):
        validation_errors = list(retry_decision.validation_errors or validation_errors)
    first = next(
        (
            issue
            for issue in validation_errors
            if hasattr(issue, "message")
        ),
        None,
    )
    if first is not None:
        return str(getattr(first, "message", ""))
    if retry_decision.reason:
        return str(retry_decision.reason)
    return "assistant text action is not an executable action source."


def _client_action_context(
    *,
    request: Request | None = None,
    user_id: str | None = None,
) -> dict[str, object] | None:
    context: dict[str, object] = {}
    if request is None or not user_id:
        return None
    platform = _header_value(request, "x-client-platform")
    if platform:
        context["platform"] = platform
    shell = _header_value(request, "x-client-shell")
    if shell:
        context["shell"] = shell
    default_browser = _header_value(request, "x-client-browser")
    if default_browser:
        context["default_browser"] = default_browser
    search_engine = _header_value(request, "x-client-search-engine")
    if search_engine:
        context["search_engine"] = search_engine
    calendar_provider = _header_value(request, "x-client-calendar-provider")
    if calendar_provider:
        context["calendar_provider"] = calendar_provider
    timezone = _header_value(request, "x-client-timezone")
    if timezone:
        context["timezone"] = timezone
    capabilities = _header_csv(request, "x-client-capabilities")
    enabled_capabilities = _header_csv(request, "x-client-enabled-capabilities")
    if capabilities:
        context["capabilities"] = capabilities
    if enabled_capabilities:
        context["enabled_capabilities"] = enabled_capabilities
        context["capabilities"] = _merge_capability_context(
            context.get("capabilities"),
            enabled_capabilities,
        )
    runtime_profile = _stored_runtime_profile(request=request, user_id=user_id)
    if runtime_profile:
        _merge_runtime_profile_context(context, runtime_profile)
    store = getattr(request.app.state, "action_context", None)
    browser_context = (
        store.browser_context(user_id)
        if store is not None and hasattr(store, "browser_context")
        else None
    )
    if browser_context is not None:
        context.update(
            {
                "browser_active": True,
                "last_query": browser_context.last_query,
                "last_url": browser_context.last_url,
            }
        )
    latest_result = (
        store.latest_result(user_id)
        if store is not None and hasattr(store, "latest_result")
        else None
    )
    if latest_result is not None:
        context["latest_action_result"] = _action_result_context_payload(latest_result)
    latest_observation = (
        store.latest_observation(user_id)
        if store is not None and hasattr(store, "latest_observation")
        else None
    )
    if latest_observation is not None:
        context["latest_observation"] = _action_result_context_payload(latest_observation)
    return context or None


def _latest_observation_context(
    *,
    request: Request | None = None,
    user_id: str | None = None,
) -> dict[str, object] | None:
    if request is None or not user_id:
        return None
    store = getattr(request.app.state, "action_context", None)
    latest_observation = (
        store.latest_observation(user_id)
        if store is not None and hasattr(store, "latest_observation")
        else None
    )
    if latest_observation is None:
        return None
    return _action_result_context_payload(latest_observation)


def _action_result_context_payload(value: object) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in ("action_type", "command", "status", "output"):
        item = getattr(value, key, None)
        if item is not None:
            payload[key] = item
    return payload


def _stored_runtime_profile(
    *,
    request: Request,
    user_id: str,
) -> dict[str, object]:
    cache = _runtime_profile_cache(request)
    cached = cache.get(user_id)
    if isinstance(cached, dict):
        return cached
    core_client = getattr(request.app.state, "core_client", None)
    if core_client is None or not hasattr(core_client, "get_runtime_profile"):
        return {}
    try:
        result = core_client.get_runtime_profile(user_id=user_id)
    except Exception as exc:
        logger.warning("runtime profile lookup failed user=%s error=%s", user_id, exc)
        return {}
    if isinstance(result, dict):
        cache[user_id] = result
        return result
    return {}


def _runtime_profile_cache(request: Request) -> dict[str, dict[str, object]]:
    cache = getattr(request.app.state, "runtime_profiles", None)
    if isinstance(cache, dict):
        return cache
    request.app.state.runtime_profiles = {}
    return request.app.state.runtime_profiles


def _merge_runtime_profile_context(
    context: dict[str, object],
    profile: dict[str, object],
) -> None:
    for source_key, context_key in (
        ("platform", "platform"),
        ("default_browser", "default_browser"),
        ("search_engine", "search_engine"),
    ):
        value = profile.get(source_key)
        if context_key not in context and isinstance(value, str) and value.strip():
            context[context_key] = value.strip()

    profile_capabilities = profile.get("capabilities")
    if isinstance(profile_capabilities, list | dict):
        context["capabilities"] = _merge_capability_context(
            context.get("capabilities"),
            profile_capabilities,
        )

    applications = _runtime_applications_for_context(profile.get("applications"))
    if applications:
        context["available_applications"] = applications
        context["available_application_names"] = [
            app["name"] for app in applications if isinstance(app.get("name"), str)
        ]

    terminal = profile.get("terminal")
    if isinstance(terminal, dict):
        trimmed_terminal = {
            key: value
            for key, value in terminal.items()
            if key
            in {
                "enabled",
                "shell",
                "shell_path",
                "cwd",
                "supports_pty",
                "requires_confirm",
                "timeout_seconds",
            }
        }
        context["terminal"] = trimmed_terminal
        shell = trimmed_terminal.get("shell")
        if "shell" not in context and isinstance(shell, str) and shell.strip():
            context["shell"] = shell.strip()


def _runtime_applications_for_context(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    applications: list[dict[str, object]] = []
    for item in value[:150]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        app: dict[str, object] = {"name": name.strip()}
        for key in ("display_name", "bundle_id", "executable", "kind"):
            field_value = item.get(key)
            if isinstance(field_value, str) and field_value.strip():
                app[key] = field_value.strip()
        aliases = _string_list(item.get("aliases"))
        if aliases:
            app["aliases"] = aliases[:12]
        applications.append(app)
    return applications


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _merge_capability_context(existing: object, profile_value: object) -> object:
    if isinstance(existing, dict) or isinstance(profile_value, dict):
        merged: dict[str, object] = {}
        if isinstance(existing, dict):
            merged.update(existing)
        elif isinstance(existing, list):
            for item in existing:
                if isinstance(item, str):
                    merged[item] = True
        if isinstance(profile_value, dict):
            merged.update(profile_value)
        elif isinstance(profile_value, list):
            for item in profile_value:
                if isinstance(item, str):
                    merged[item] = True
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("capability") or item.get("id")
                    if isinstance(name, str) and name.strip():
                        merged[name.strip()] = item
        return merged

    merged_list: list[object] = []
    for source in (existing, profile_value):
        if not isinstance(source, list):
            continue
        for item in source:
            if item not in merged_list:
                merged_list.append(item)
    return merged_list


def _header_value(request: Request, name: str) -> str | None:
    raw = request.headers.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _header_csv(request: Request, name: str) -> list[str]:
    raw = request.headers.get(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _client_action_decision(
    message: str,
    *,
    request: Request | None = None,
    user_id: str | None = None,
    validation_errors: list[object] | None = None,
) -> ActionIntentDecision | None:
    if not should_try_client_action_classifier(message):
        return ActionIntentDecision(
            should_act=False,
            execution_mode="no_action",
            intent=None,
            confidence=1.0,
            reason="empty message",
            actions=[],
        )
    context = _client_action_context(request=request, user_id=user_id)
    latest_observation = _latest_observation_context(request=request, user_id=user_id)
    decision = classify_client_action_intent_decision(
        message,
        context=context,
        latest_observation=latest_observation,
        validation_errors=validation_errors,  # type: ignore[arg-type]
    )
    if decision is not None:
        return decision
    return _client_action_decision_via_core_model(
        message,
        request=request,
        user_id=user_id,
        context=context,
        validation_errors=validation_errors,
    )


def _client_action_decision_via_core_model(
    message: str,
    *,
    request: Request | None,
    user_id: str | None,
    context: dict[str, object] | None,
    validation_errors: list[object] | None = None,
) -> ActionIntentDecision | None:
    if request is None or not user_id:
        return None
    core_client = getattr(request.app.state, "core_client", None)
    if core_client is None or not hasattr(core_client, "chat_request"):
        return None
    typed_errors = [
        issue
        for issue in (validation_errors or [])
        if hasattr(issue, "model_dump")
    ]
    try:
        response = core_client.chat_request(
            message=action_compiler_prompt_payload(
                message=message,
                context=context,
                validation_errors=typed_errors,  # type: ignore[arg-type]
            ),
            task_type="execution",
            confirm=False,
            route_override="realtime",
            user_id=user_id,
            user_email="",
            request_id="",
        )
    except Exception as exc:
        logger.warning("core model action compiler fallback failed: %s", exc)
        return None
    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None
    decision = compile_action_decision_from_model_text(content, context=context)
    if decision is not None:
        logger.info(
            "core model action compiler fallback produced mode=%s actions=%d message=%s",
            decision.execution_mode,
            len(decision.actions),
            message[:160],
        )
    return decision


def _action_intent_payload(
    decision: ActionIntentDecision | None,
    *,
    unavailable: bool = False,
) -> dict[str, object]:
    if decision is None:
        return {
            "should_act": False,
            "execution_mode": "unavailable",
            "intent": None,
            "confidence": 0.0,
            "reason": "sLLM action classifier unavailable" if unavailable else None,
            "action_count": 0,
        }
    return {
        "should_act": decision.should_act,
        "execution_mode": decision.execution_mode,
        "intent": decision.intent,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "action_count": len(decision.actions),
    }


def _is_direct_action_decision(decision: ActionIntentDecision | None) -> bool:
    return (
        decision is not None
        and decision.execution_mode in DIRECT_EXECUTION_MODES
        and bool(decision.actions)
    )


def _action_decision_reason(decision: ActionIntentDecision) -> str:
    return str(decision.reason or decision.intent or decision.execution_mode)


def _stream_direct_action_decision(
    *,
    decision: ActionIntentDecision,
    message: str,
    request_id: str,
    user_id: str,
    action_dispatcher,
) -> Generator[bytes, None, None]:
    logger.info(
        "action direct dispatch request_id=%s mode=%s actions=%d message=%s",
        request_id,
        decision.execution_mode,
        len(decision.actions),
        message[:200],
    )
    yield _sse_event(
        "classification",
        {
            "category": "general",
            "mode": ConversationMode.REALTIME.value,
            "confidence": decision.confidence,
            "reasons": ["direct client action"],
        },
    )
    yield _sse_event(
        "thinking",
        {
            "text": "클라이언트 액션을 준비중...",
            "mode": ConversationMode.REALTIME.value,
        },
    )
    yield from _stream_dispatched_actions(
        actions=decision.actions,
        request_id=request_id,
        user_id=user_id,
        action_dispatcher=action_dispatcher,
        done_content="요청한 작업을 실행했습니다.",
        done_summary="direct client action dispatched",
    )


def _deepthink_step_payload(step) -> dict[str, object]:
    return {"id": step.id, "title": step.title, "description": step.description}


def _append_deepthink_step_context(
    execution_context: list[str],
    step_results,
) -> None:
    for step_result in step_results:
        execution_context.append(
            f"- {step_result.title}: {step_result.content[:500]}"
        )


def _dispatch_deepthink_actions_sync(
    *,
    actions: list[ClientAction],
    action_dispatcher,
    request_id: str,
    user_id: str,
    execution_context: list[str],
) -> tuple[list[ClientAction], list[dict[str, object]]]:
    all_actions: list[ClientAction] = []
    action_results: list[dict[str, object]] = []
    pending_actions = list(actions)
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
    return all_actions, action_results


def _stream_deepthink_actions(
    *,
    actions: list[ClientAction],
    action_dispatcher,
    request_id: str,
    user_id: str,
    execution_context: list[str],
    all_actions: list[ClientAction],
    action_results: list[dict[str, object]],
) -> Generator[bytes, None, None]:
    pending_actions = list(actions)
    while pending_actions:
        action = pending_actions.pop(0)
        all_actions.append(action)
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
            plan_steps=[_deepthink_step_payload(step)],
            user_id=user_id,
            execution_context=execution_context,
        )
        step_results.extend(exec_resp.steps)
        step_actions, step_action_results = _dispatch_deepthink_actions_sync(
            actions=exec_resp.actions,
            action_dispatcher=action_dispatcher,
            request_id=request_id,
            user_id=user_id,
            execution_context=execution_context,
        )
        all_actions.extend(step_actions)
        action_results.extend(step_action_results)
        _append_deepthink_step_context(execution_context, exec_resp.steps)

    completed = [s for s in step_results if s.status == "completed"]
    summary = f"{len(completed)}/{len(step_results)} 단계 완료"
    content = "\n\n".join(f"### {s.title}\n{s.content}" for s in step_results)
    content, summary = _merge_action_completion_into_response(
        content=content,
        summary=summary,
        action_results=action_results,
    )
    return step_results, summary, content, all_actions, action_results


def _merge_action_completion_into_response(
    *,
    content: str,
    summary: str,
    action_results: list[dict[str, object]],
) -> tuple[str, str]:
    if not action_results:
        return content, summary
    action_content, action_summary = _action_completion_message(
        action_results,
        success_content=content,
        success_summary=summary,
    )
    if action_content == content and action_summary == summary:
        return content, summary
    return f"{content}\n\n{action_content}".strip(), action_summary


def _stream_orchestrated_conversation(
    req: ConversationRequest,
    request: Request,
    principal,
) -> Generator[bytes, None, None]:
    request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"

    action_executor: concurrent.futures.ThreadPoolExecutor | None = None
    action_future: concurrent.futures.Future[ActionIntentDecision | None] | None = None
    route_executor: concurrent.futures.ThreadPoolExecutor | None = None
    route_future: concurrent.futures.Future[RoutingDecision] | None = None
    if req.override != ContractConversationMode.PLANNING:
        action_executor, action_future = _start_action_decision_future(
            req.message,
            request=request,
            user_id=principal.user_id,
        )
    routing_context = ConversationContext(
        recent_failures=req.recent_failures,
        ambiguity_count=req.ambiguity_count,
        turn_index=req.turn_index,
    )
    route_override = req.override.value if req.override else None

    if route_override in (None, ConversationMode.REALTIME.value):
        if route_override is None:
            route_executor, route_future = _start_routing_decision_future(
                req.message,
                override=None,
                context=routing_context,
            )
        try:
            stream = request.app.state.core_client.chat_stream(
                message=req.message,
                task_type="general",
                confirm=False,
                route_override="realtime",
                user_id=principal.user_id,
                user_email=getattr(principal, "email", ""),
                request_id=request_id,
            )
            yield from _stream_realtime_with_parallel_decisions(
                stream,
                action_future=action_future,
                route_future=route_future,
                request_id=request_id,
                message=req.message,
                user_id=principal.user_id,
                action_dispatcher=request.app.state.action_dispatcher,
                context=_client_action_context(
                    request=request,
                    user_id=principal.user_id,
                ),
            )
        finally:
            if action_future is not None:
                action_future.cancel()
            if action_executor is not None:
                action_executor.shutdown(wait=False, cancel_futures=True)
            if route_future is not None:
                route_future.cancel()
            if route_executor is not None:
                route_executor.shutdown(wait=False, cancel_futures=True)
        return

    decision = evaluate_conversation_mode(
        req.message,
        override=route_override,
        context=routing_context,
    )
    _log_classification(req.message, decision.mode, decision.confidence)

    for chunk in _classification_chunks_for_decision(decision):
        yield chunk

    action_decision: ActionIntentDecision | None = None
    if action_future is not None:
        try:
            action_decision = action_future.result()
        finally:
            if action_executor is not None:
                action_executor.shutdown(wait=False, cancel_futures=True)
        yield _sse_event(
            "action_intent",
            _action_intent_payload(
                action_decision,
                unavailable=action_decision is None,
            ),
        )
        if _is_direct_action_decision(action_decision):
            yield from _stream_direct_action_decision(
                decision=action_decision,
                message=req.message,
                request_id=request_id,
                user_id=principal.user_id,
                action_dispatcher=request.app.state.action_dispatcher,
            )
            return

        if (
            action_decision is not None
            and action_decision.execution_mode == "needs_plan"
            and req.override is None
        ):
            decision = RoutingDecision(
                mode=ConversationMode.DEEP,
                triggered=True,
                confidence=action_decision.confidence,
                reasons=[
                    "action intent requires planning: "
                    f"{action_decision.reason or action_decision.intent or 'needs_plan'}"
                ],
            )

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
                plan_steps=[_deepthink_step_payload(step)],
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

            yield from _stream_deepthink_actions(
                actions=result.actions,
                action_dispatcher=request.app.state.action_dispatcher,
                request_id=request_id,
                user_id=principal.user_id,
                execution_context=execution_context,
                all_actions=all_actions,
                action_results=action_results,
            )
            _append_deepthink_step_context(execution_context, result.steps)

        content = "\n\n".join(
            f"### {s.title}\n{s.content}" for s in step_results
        )
        completed_count = len([s for s in step_results if s.status == "completed"])
        summary = f"{completed_count}/{len(step_results)} 단계 완료"
        content, summary = _merge_action_completion_into_response(
            content=content,
            summary=summary,
            action_results=action_results,
        )

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
        if should_try_client_action_classifier(req.message):
            yield from _stream_with_embedded_action_intercept(
                stream,
                request_id=request_id,
                message=req.message,
                user_id=principal.user_id,
                action_dispatcher=request.app.state.action_dispatcher,
                context=_client_action_context(
                    request=request,
                    user_id=principal.user_id,
                ),
            )
        else:
            yield from _stream_with_model_logging(
                stream,
                request_id=request_id,
                message=req.message,
            )


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
    principal = request.state.principal
    core_client = request.app.state.core_client
    request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"

    if req.override != ContractConversationMode.PLANNING:
        action_decision = _client_action_decision(
            req.message,
            request=request,
            user_id=principal.user_id,
        )
        if _is_direct_action_decision(action_decision):
            logger.info(
                "action direct dispatch request_id=%s mode=%s actions=%d message=%s",
                request_id,
                action_decision.execution_mode,
                len(action_decision.actions),
                req.message[:200],
            )
            actions, _action_results = _dispatch_actions_sync(
                actions=action_decision.actions,
                request_id=request_id,
                user_id=principal.user_id,
                action_dispatcher=request.app.state.action_dispatcher,
            )
            content, summary = _action_completion_message(
                _action_results,
                success_content="요청한 작업을 실행했습니다.",
                success_summary="direct client action dispatched",
            )
            return ConversationResponse(
                mode=ContractConversationMode.REALTIME,
                triggered=True,
                confidence=action_decision.confidence,
                reasons=["direct client action: " + _action_decision_reason(action_decision)],
                handler="client-action",
                content=content,
                summary=summary,
                next_actions=[],
                actions=actions,
            )

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
        action_executor: concurrent.futures.ThreadPoolExecutor | None = None
        action_future: concurrent.futures.Future[ActionIntentDecision | None] | None = None
        if should_try_client_action_classifier(req.message):
            action_executor, action_future = _start_action_decision_future(
                req.message,
                request=request,
                user_id=principal.user_id,
            )

        try:
            stream = request.app.state.core_client.chat_stream(
                message=req.message,
                task_type=req.task_type,
                confirm=req.confirm,
                route_override=req.thinking_mode,
                user_id=principal.user_id,
                user_email=getattr(principal, "email", ""),
                request_id=request_id,
            )
            if should_try_client_action_classifier(req.message):
                yield from _stream_realtime_with_action_arbitration(
                    stream,
                    action_future=action_future,
                    request_id=request_id,
                    message=req.message,
                    user_id=principal.user_id,
                    action_dispatcher=request.app.state.action_dispatcher,
                    context=_client_action_context(
                        request=request,
                        user_id=principal.user_id,
                    ),
                )
            else:
                yield from _stream_with_model_logging(
                    stream,
                    request_id=request_id,
                    message=req.message,
                )
        finally:
            if action_future is not None:
                action_future.cancel()
            if action_executor is not None:
                action_executor.shutdown(wait=False, cancel_futures=True)
        return

    action_executor, action_future = _start_action_decision_future(
        req.message,
        request=request,
        user_id=principal.user_id,
    )
    route_executor, route_future = _start_routing_decision_future(req.message)

    try:
        stream = request.app.state.core_client.chat_stream(
            message=req.message,
            task_type=req.task_type,
            confirm=req.confirm,
            route_override="realtime",
            user_id=principal.user_id,
            user_email=getattr(principal, "email", ""),
            request_id=request_id,
        )
        yield from _stream_realtime_with_parallel_decisions(
            stream,
            action_future=action_future,
            route_future=route_future,
            request_id=request_id,
            message=req.message,
            user_id=principal.user_id,
            action_dispatcher=request.app.state.action_dispatcher,
            context=_client_action_context(
                request=request,
                user_id=principal.user_id,
            ),
        )
    finally:
        action_future.cancel()
        action_executor.shutdown(wait=False, cancel_futures=True)
        route_future.cancel()
        route_executor.shutdown(wait=False, cancel_futures=True)
    return



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


@api_router.put(
    "/chat/model-config/{model_config_id}",
    tags=["chat"],
    summary="Update model config",
)
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


@api_router.put(
    "/client/runtime-profile",
    tags=["execution"],
    summary="Save client runtime profile",
)
def upsert_client_runtime_profile(
    req: RuntimeProfileRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    result = request.app.state.core_client.set_runtime_profile(
        user_id=principal.user_id,
        body=req.model_dump(),
    )
    _runtime_profile_cache(request)[principal.user_id] = result
    return result


@api_router.get(
    "/client/runtime-profile",
    tags=["execution"],
    summary="Get client runtime profile",
)
def get_client_runtime_profile(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    result = request.app.state.core_client.get_runtime_profile(user_id=principal.user_id)
    _runtime_profile_cache(request)[principal.user_id] = result
    return result


@api_router.get(
    "/client/actions/registry",
    tags=["execution"],
    summary="Fetch canonical client action type registry",
)
def client_action_registry(
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    return action_registry_payload()


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
