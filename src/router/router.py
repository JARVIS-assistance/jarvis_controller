import concurrent.futures
import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Generator
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote_plus
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
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
    TodoCreateRequest,
    TodoUpdateRequest,
    VerifyRequest,
    action_registry_payload,
)
from jarvis_contracts import (
    ConversationMode as ContractConversationMode,
)
from pydantic import BaseModel, Field, field_validator, model_validator

from planner.action_adapter import V2ToV1ActionAdapter
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
    dispatch_actions_sync as _dispatch_actions_sync,
)
from planner.action_pipeline import (
    stream_action_dispatch_events as _stream_action_dispatch_events,
)
from planner.action_pipeline import (
    stream_dispatched_actions as _stream_dispatched_actions,
)
from planner.action_templates import (
    materialize_explicit_app_open_for_text,
    materialize_fresh_context_app_open_for_text,
    normalize_browser_search_query,
)
from planner.autonomous_intent import autonomous_loop_requested
from planner.autonomous_loop import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_SECONDS,
    run_autonomous_loop,
    stream_autonomous_loop,
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
from planner.runtime_profile_enricher import enrich_runtime_profile_applications

from .intent_todo import (
    _extract_todo_due_at,
    _free_time_check_requested,
    _strip_todo_time_phrase,
    _todo_create_action_from_message,
    _todo_create_requested,
    _todo_datetime_from_match,
    _todo_delete_action_from_message,
    _todo_delete_query,
    _todo_delete_requested,
    _todo_due_hour_candidates,
    _todo_list_action_from_message,
    _todo_list_requested,
    _todo_time_from_message,
    _todo_title_and_due_at,
)
from .text_match import _normalized_action_match_key

logger = logging.getLogger("jarvis_controller")

api_router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
LEGACY_TTS_VOICES = {
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "onyx",
    "nova",
    "sage",
    "shimmer",
}
PCM_DEFAULT_VOICE_ALIASES = {"", "default", "marin"}
PCM_DEFAULT_MODEL_ALIASES = {"", "gpt-4o-mini-tts", "tts-1", "tts-1-hd"}
_ACTION_ARBITRATION_BUFFER_SECONDS = "JARVIS_ACTION_ARBITRATION_BUFFER_SECONDS"
_ACTION_ARBITRATION_DEFAULT_SECONDS = 0.0
_ACTION_INTENT_CORE_FALLBACK_ENABLED = "JARVIS_ACTION_INTENT_CORE_FALLBACK_ENABLED"
_ACTION_INTENT_DONE_GRACE_SECONDS = "JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS"
_ACTION_INTENT_DONE_GRACE_DEFAULT_SECONDS = 0.8
_ACTION_CANDIDATE_WAIT_SECONDS = "JARVIS_ACTION_CANDIDATE_WAIT_SECONDS"
_ACTION_CANDIDATE_WAIT_DEFAULT_SECONDS = 1.6
_ACTION_ACK_RECOVERY_GRACE_SECONDS = "JARVIS_ACTION_ACK_RECOVERY_GRACE_SECONDS"
_ACTION_ACK_RECOVERY_GRACE_DEFAULT_SECONDS = 8.0
_ACTION_CONTEXT_APPLICATION_LIMIT = "JARVIS_ACTION_CONTEXT_APPLICATION_LIMIT"
_ACTION_CONTEXT_APPLICATION_DEFAULT_LIMIT = 250
_ACTION_CONTEXT_TRIMMED_APPLICATION_NAME_LIMIT = (
    "JARVIS_ACTION_CONTEXT_TRIMMED_APPLICATION_NAME_LIMIT"
)
_ACTION_CONTEXT_TRIMMED_APPLICATION_NAME_DEFAULT_LIMIT = 250
_ACTION_ACK = "진행하겠습니다!"
_RECENT_USER_MESSAGE_TTL_SECONDS = 300.0
_LIVE_TTS_WAIT_SECONDS = 120.0
_LIVE_TTS_MIN_SEGMENT_CHARS = 36
_LIVE_TTS_MAX_SEGMENT_CHARS = 90
_LIVE_TTS_SOFT_SEGMENT_CHARS = 54
_LIVE_TTS_SENTENCE_RE = re.compile(r"(.+?[.!?。！？…]|.+?[.!?]['\")\]]+)(\s+|$)", re.DOTALL)
_LIVE_TTS_SOFT_BREAK_RE = re.compile(r"^(.+?[,，、;；:：]|.+?(?:요|다|죠|네|니다|습니다)[,，]?)(\s+|$)", re.DOTALL)
_LIVE_TTS_SESSIONS: dict[str, "_LiveTtsSession"] = {}
_LOCAL_APP_ALIAS_PROFILE: dict[str, dict[str, tuple[str, ...]]] = {
    "com.apple.stocks": {
        "aliases": ("주식", "주식앱", "증권", "Stocks", "stocks"),
        "capabilities": ("stock", "stocks", "finance", "market", "주식", "증권"),
        "categories": ("finance", "stocks"),
        "keywords": ("주식 시세", "증권", "시장"),
    },
    "com.apple.weather": {
        "aliases": ("날씨", "날씨앱", "Weather", "weather"),
        "capabilities": ("weather", "forecast", "날씨", "예보"),
        "categories": ("weather",),
        "keywords": ("오늘 날씨", "지역 날씨"),
    },
}
_ACTION_OBJECT_TERMS = (
    "browser",
    "chrome",
    "safari",
    "브라우저",
    "크롬",
    "사파리",
    "앱",
    "어플",
    "메모장",
    "텍스트 편집기",
    "application",
    "app",
    "터미널",
    "terminal",
    "cmd",
    "콘솔",
    "console",
    "명령 프롬프트",
    "할일",
    "할 일",
    "todo",
    "to-do",
    "파일",
    "file",
    "폴더",
    "folder",
    "다운로드",
    "downloads",
    "화면",
    "screen",
    "스크린샷",
    "screenshot",
    "캡처",
    "capture",
    "클립보드",
    "clipboard",
    "마우스",
    "mouse",
    "키보드",
    "keyboard",
)
_ACTION_VERB_TERMS = (
    "열어",
    "켜",
    "실행",
    "들어가",
    "검색",
    "찾아",
    "클릭",
    "눌러",
    "입력",
    "작성",
    "타이핑",
    "복사",
    "붙여",
    "캡처",
    "찍어",
    "읽어",
    "봐줘",
    "보여",
    "확인",
    "open",
    "launch",
    "run",
    "search",
    "find",
    "click",
    "type",
    "copy",
    "paste",
    "show",
    "list",
)
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
    tts_enabled: bool = False
    tts_voice: str = Field(default="default", max_length=80)
    tts_model: str | None = Field(default=None, max_length=4096)
    tts_sample_rate: int = Field(default=24000, ge=8000, le=48000)
    tts_channels: Literal[1, 2] = 1
    tts_sample_width: Literal[2] = 2


class ChatResponse(BaseModel):
    request_id: str
    route: str
    provider_mode: str
    provider_name: str
    model_name: str
    content: str


class TextToSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    provider: Literal["openai", "local"] = "openai"
    model: Literal["gpt-4o-mini-tts", "tts-1", "tts-1-hd"] = "gpt-4o-mini-tts"
    voice: Literal[
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    ] = "marin"
    response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    instructions: str | None = Field(default=None, max_length=1200)
    speed: float | None = Field(default=None, ge=0.25, le=4.0)

    @model_validator(mode="after")
    def validate_model_voice(self) -> "TextToSpeechRequest":
        if self.model in {"tts-1", "tts-1-hd"} and self.voice not in LEGACY_TTS_VOICES:
            raise ValueError(f"{self.model} does not support voice {self.voice!r}")
        return self


class TextToSpeechChunk(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


class TextToSpeechPCMRequest(BaseModel):
    chunks: list[TextToSpeechChunk] = Field(min_length=1, max_length=64)
    voice: str = Field(default="default", max_length=80)
    model: str | None = Field(default=None, max_length=4096)
    sample_rate: int = Field(default=24000, ge=8000, le=48000)
    channels: Literal[1, 2] = 1
    sample_width: Literal[2] = 2
    format: Literal["pcm_s16le"] = "pcm_s16le"

    @field_validator("voice", mode="before")
    @classmethod
    def normalize_voice(cls, value: Any) -> str:
        voice = "" if value is None else str(value).strip()
        if voice.lower() in PCM_DEFAULT_VOICE_ALIASES:
            return "default"
        return voice

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, value: Any) -> str | None:
        model = "" if value is None else str(value).strip()
        if model.lower() in PCM_DEFAULT_MODEL_ALIASES:
            return None
        return model


class ConversationCancelRequest(BaseModel):
    request_id: str | None = None
    reason: str = "barge_in"


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
    capabilities: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
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
    allowed_commands: list[str] = Field(default_factory=list)
    allowed_cwds: list[str] = Field(default_factory=list)
    supports_pty: bool = False
    requires_confirm: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class RuntimeProfileRequest(BaseModel):
    platform: str | None = Field(default=None, max_length=40)
    default_browser: str | None = Field(default=None, max_length=80)
    capabilities: list[str] = Field(default_factory=list)
    enabled_capabilities: list[str] = Field(default_factory=list)
    supported_capabilities: list[str] = Field(default_factory=list)
    applications: list[RuntimeApplicationRequest] = Field(default_factory=list)
    terminal: TerminalProfileRequest | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutonomousLoopRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1, le=25)
    max_seconds: float = Field(default=DEFAULT_MAX_SECONDS, ge=5.0, le=1800.0)


class VisionFramePushRequest(BaseModel):
    frame_base64: str = Field(min_length=1, max_length=8_000_000)
    mime_type: str = Field(default="image/jpeg", max_length=40)
    captured_at: str | None = Field(default=None, max_length=64)
    sequence: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


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


class _LiveTtsSession:
    def __init__(
        self,
        *,
        session_id: str,
        request_id: str,
        user_id: str,
        config: dict[str, object],
    ) -> None:
        self.session_id = session_id
        self.request_id = request_id
        self.user_id = user_id
        self.config = config
        self.text_queue: queue.Queue[str | None] = queue.Queue()
        self.closed = False


def _live_tts_config(req: object) -> dict[str, object]:
    body: dict[str, object] = {
        "voice": getattr(req, "tts_voice", "default") or "default",
        "sample_rate": getattr(req, "tts_sample_rate", 24000),
        "channels": getattr(req, "tts_channels", 1),
        "sample_width": getattr(req, "tts_sample_width", 2),
        "format": "pcm_s16le",
    }
    model = getattr(req, "tts_model", None)
    if model:
        body["model"] = model
    return body


def _create_live_tts_session(
    *,
    req: object,
    request_id: str,
    user_id: str,
) -> _LiveTtsSession:
    session_id = f"tts_{uuid4().hex}"
    session = _LiveTtsSession(
        session_id=session_id,
        request_id=request_id,
        user_id=user_id,
        config=_live_tts_config(req),
    )
    _LIVE_TTS_SESSIONS[session_id] = session
    return session


def _finish_live_tts_session(session: _LiveTtsSession) -> None:
    if session.closed:
        return
    session.closed = True
    session.text_queue.put(None)


def _split_live_tts_segments(buffer: str) -> tuple[list[str], str]:
    segments: list[str] = []
    remaining = buffer
    pending = ""
    while remaining:
        match = _LIVE_TTS_SENTENCE_RE.match(remaining)
        if match:
            segment = match.group(1).strip()
            if segment:
                pending = f"{pending} {segment}".strip()
                if len(pending) >= _LIVE_TTS_MIN_SEGMENT_CHARS:
                    segments.append(pending)
                    pending = ""
            remaining = remaining[match.end() :].lstrip()
            continue
        soft_match = _LIVE_TTS_SOFT_BREAK_RE.match(remaining)
        if soft_match:
            candidate = f"{pending} {soft_match.group(1).strip()}".strip()
            if len(candidate) >= _LIVE_TTS_SOFT_SEGMENT_CHARS:
                segments.append(candidate)
                pending = ""
                remaining = remaining[soft_match.end() :].lstrip()
                continue
        candidate = f"{pending} {remaining}".strip()
        if len(candidate) >= _LIVE_TTS_MAX_SEGMENT_CHARS:
            split_at = max(
                remaining.rfind(" ", 0, _LIVE_TTS_MAX_SEGMENT_CHARS),
                remaining.rfind(",", 0, _LIVE_TTS_MAX_SEGMENT_CHARS),
                remaining.rfind("，", 0, _LIVE_TTS_MAX_SEGMENT_CHARS),
                remaining.rfind("、", 0, _LIVE_TTS_MAX_SEGMENT_CHARS),
            )
            if split_at < _LIVE_TTS_MIN_SEGMENT_CHARS:
                split_at = _LIVE_TTS_MAX_SEGMENT_CHARS
            segment = f"{pending} {remaining[:split_at]}".strip()
            if segment:
                segments.append(segment)
            pending = ""
            remaining = remaining[split_at:].lstrip()
            continue
        break
    return segments, f"{pending} {remaining}".strip()


def _stream_with_live_tts(
    stream: Generator[bytes, None, None],
    *,
    req: object,
    request_id: str,
    user_id: str,
) -> Generator[bytes, None, None]:
    if not bool(getattr(req, "tts_enabled", False)):
        yield from stream
        return

    session = _create_live_tts_session(req=req, request_id=request_id, user_id=user_id)
    saw_delta = False
    yield _sse_event(
        "tts_session",
        {
            "session_id": session.session_id,
            "request_id": request_id,
            "stream_url": f"/audio/speech/live/{session.session_id}",
            "format": "pcm_s16le",
            "sample_rate": session.config["sample_rate"],
            "channels": session.config["channels"],
            "sample_width": session.config["sample_width"],
        },
    )

    try:
        for chunk in stream:
            for event_name, payload in _sse_payloads_from_chunk(chunk):
                if event_name == "assistant_delta":
                    content = payload.get("content")
                    if (
                        isinstance(content, str)
                        and content.strip()
                        and content.strip() != _ACTION_ACK.strip()
                    ):
                        saw_delta = True
                        session.text_queue.put(content)
                elif event_name in {"assistant_done", "conversation.done", "done"}:
                    content = payload.get("content") or payload.get("text")
                    if (
                        isinstance(content, str)
                        and content.strip()
                        and not saw_delta
                        and content.strip() != _ACTION_ACK.strip()
                    ):
                        session.text_queue.put(content)
                    _finish_live_tts_session(session)
            yield chunk
    finally:
        _finish_live_tts_session(session)


def _stream_live_tts_segment(
    *,
    session: _LiveTtsSession,
    request: Request,
    chunk_id: str,
    text: str,
) -> Generator[bytes, None, None]:
    body = dict(session.config)
    body["chunks"] = [{"id": chunk_id, "text": text}]
    result = request.app.state.core_client.synthesize_speech_pcm_stream(
        user_id=session.user_id,
        body=body,
        request_id=session.request_id,
    )
    yield from result.body


def _stream_live_tts_pcm(
    *,
    session: _LiveTtsSession,
    request: Request,
) -> Generator[bytes, None, None]:
    buffer = ""
    index = 0
    try:
        while True:
            try:
                text = session.text_queue.get(timeout=_LIVE_TTS_WAIT_SECONDS)
            except queue.Empty:
                break
            if text is None:
                break
            buffer += text
            segments, buffer = _split_live_tts_segments(buffer)
            for segment in segments:
                index += 1
                yield from _stream_live_tts_segment(
                    session=session,
                    request=request,
                    chunk_id=f"{session.session_id}:{index}",
                    text=segment,
                )

        final_text = buffer.strip()
        if final_text:
            yield from _stream_live_tts_segment(
                session=session,
                request=request,
                chunk_id=f"{session.session_id}:final",
                text=final_text,
            )
    finally:
        _LIVE_TTS_SESSIONS.pop(session.session_id, None)


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


def _action_intent_core_fallback_enabled() -> bool:
    return os.getenv(_ACTION_INTENT_CORE_FALLBACK_ENABLED, "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _action_runtime_config_payload() -> dict[str, object]:
    return {
        "intent_model_enabled": os.getenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1"),
        "provider": (
            os.getenv("JARVIS_ACTION_MODEL_PROVIDER")
            or os.getenv("JARVIS_INTERNAL_MODEL_PROVIDER")
            or "openai_compat"
        ),
        "endpoint": (
            os.getenv("JARVIS_ACTION_MODEL_ENDPOINT")
            or os.getenv("JARVIS_ACTION_INTENT_MODEL_ENDPOINT")
            or "https://qwen.breakpack.cc/engines/v1/chat/completions"
        ),
        "intent_model": (
            os.getenv("JARVIS_ACTION_INTENT_MODEL_NAME")
            or os.getenv("JARVIS_ACTION_INTENT_MODEL")
            or "docker.io/ai/qwen2.5:1.5B-F16"
        ),
        "compiler_model": (
            os.getenv("JARVIS_ACTION_COMPILER_MODEL_NAME")
            or os.getenv("JARVIS_ACTION_COMPILER_MODEL")
            or os.getenv("JARVIS_ACTION_PLAN_MODEL_NAME")
            or os.getenv("JARVIS_ACTION_PLAN_MODEL")
            or "docker.io/ai/gemma4:E4B"
        ),
        "core_fallback_enabled": _action_intent_core_fallback_enabled(),
    }


def _action_intent_done_grace_seconds() -> float:
    configured = max(
        0.0,
        _float_env(
            _ACTION_INTENT_DONE_GRACE_SECONDS,
            _ACTION_INTENT_DONE_GRACE_DEFAULT_SECONDS,
        ),
    )
    cap = max(0.0, _float_env("JARVIS_ACTION_INTENT_DONE_GRACE_CAP_SECONDS", 0.45))
    return min(configured, cap)


def _action_candidate_wait_seconds() -> float:
    configured = max(
        0.0,
        _float_env(
            _ACTION_CANDIDATE_WAIT_SECONDS,
            _ACTION_CANDIDATE_WAIT_DEFAULT_SECONDS,
        ),
    )
    cap = max(0.0, _float_env("JARVIS_ACTION_CANDIDATE_WAIT_CAP_SECONDS", 2.5))
    return min(configured, cap)


def _action_ack_recovery_grace_seconds() -> float:
    configured = max(
        0.0,
        _float_env(
            _ACTION_ACK_RECOVERY_GRACE_SECONDS,
            _ACTION_ACK_RECOVERY_GRACE_DEFAULT_SECONDS,
        ),
    )
    cap = max(0.0, _float_env("JARVIS_ACTION_ACK_RECOVERY_GRACE_CAP_SECONDS", 8.0))
    return min(configured, cap)


def _action_context_application_limit() -> int:
    return max(
        30,
        int(_float_env(
            _ACTION_CONTEXT_APPLICATION_LIMIT,
            _ACTION_CONTEXT_APPLICATION_DEFAULT_LIMIT,
        )),
    )


def _action_context_trimmed_application_name_limit() -> int:
    return max(
        30,
        int(_float_env(
            _ACTION_CONTEXT_TRIMMED_APPLICATION_NAME_LIMIT,
            _ACTION_CONTEXT_TRIMMED_APPLICATION_NAME_DEFAULT_LIMIT,
        )),
    )


def _sse_payloads_from_chunk(chunk: bytes) -> list[tuple[str, dict[str, object]]]:
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return []

    payloads: list[tuple[str, dict[str, object]]] = []
    event_name: str | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
            data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
            continue
        if line != "" or event_name is None:
            continue
        try:
            payload = json.loads("\n".join(data_lines) or "{}")
        except json.JSONDecodeError:
            event_name = None
            data_lines = []
            continue
        payloads.append((event_name, payload))
        event_name = None
        data_lines = []

    if event_name is not None and data_lines:
        try:
            payload = json.loads("\n".join(data_lines) or "{}")
        except json.JSONDecodeError:
            return payloads
        payloads.append((event_name, payload))
    return payloads


def _assistant_chunk_content(
    chunk: bytes,
    *,
    event_names: set[str],
) -> str | None:
    for event_name, payload in _sse_payloads_from_chunk(chunk):
        if event_name not in event_names:
            continue
        content = payload.get("content")
        if content is None:
            content = payload.get("text")
        if isinstance(content, str):
            return content
    return None


def _is_assistant_done_chunk(chunk: bytes) -> bool:
    return any(
        event_name in {"assistant_done", "conversation.done", "done"}
        for event_name, _payload in _sse_payloads_from_chunk(chunk)
    )


def _is_action_ack_delta_chunk(chunk: bytes) -> bool:
    content = _assistant_chunk_content(chunk, event_names={"assistant_delta"})
    return isinstance(content, str) and content.strip() == _ACTION_ACK.strip()


def _is_action_ack_done_chunk(chunk: bytes) -> bool:
    content = _assistant_chunk_content(
        chunk,
        event_names={"assistant_done", "conversation.done", "done"},
    )
    return isinstance(content, str) and content.strip() == _ACTION_ACK.strip()


def _is_embedded_action_handling_chunk(chunk: bytes) -> bool:
    for event_name, payload in _sse_payloads_from_chunk(chunk):
        if event_name in {
            "action_compile_retry",
            "action_dispatch",
            "action_result",
            "actions",
        }:
            return True
        summary = payload.get("summary")
        if (
            event_name in {"assistant_done", "conversation.done", "done"}
            and isinstance(summary, str)
            and summary.startswith("embedded action ")
        ):
            return True
    return False


def _action_ack_suppressed_done() -> bytes:
    return _sse_event(
        "assistant_done",
        {
            "content": "액션 판단이 지연되어 실행하지 않았습니다.",
            "summary": "action ack suppressed before dispatch",
            "status": "timeout",
            "has_actions": False,
            "action_count": 0,
            "action_results": [],
            "failure_reason": "action_decision_timeout",
        },
    )


def _stream_without_leading_action_ack(
    stream: Generator[bytes, None, None],
) -> Generator[bytes, None, None]:
    buffered = ""
    suppressing = True
    for chunk in stream:
        payloads = _sse_payloads_from_chunk(chunk)
        if (
            suppressing
            and len(payloads) == 1
            and payloads[0][0] == "assistant_delta"
        ):
            _event_name, payload = payloads[0]
            content = payload.get("content")
            if not isinstance(content, str):
                suppressing = False
                yield chunk
                continue
            candidate = buffered + content
            if _ACTION_ACK.startswith(candidate):
                buffered = candidate
                continue
            if candidate.startswith(_ACTION_ACK):
                suppressing = False
                remainder = candidate[len(_ACTION_ACK) :].lstrip()
                if remainder:
                    payload["content"] = remainder
                    yield _sse_event("assistant_delta", payload)
                continue
            suppressing = False
            if buffered:
                payload["content"] = candidate
                yield _sse_event("assistant_delta", payload)
            else:
                yield chunk
            continue
        yield chunk


def _looks_like_direct_client_action_request(
    message: str,
    *,
    context: dict[str, object] | None = None,
) -> bool:
    text = message.strip()
    if not text:
        return False
    if _looks_like_meta_design_or_analysis_request(text):
        return False
    if _template_app_open_decision_from_text(text, context=context) is not None:
        return True
    if _open_url_from_message(text) is not None:
        return True
    if _browser_result_index_from_message(text) is not None:
        return True
    folded = text.casefold()
    if _browser_open_only_requested(folded, text):
        return True
    if _browser_close_tab_requested(folded):
        return True
    if _browser_search_query_from_message(text, context=context) is not None:
        return True
    if _terminal_command_from_message(text, context=context) is not None:
        return True
    if any(term in folded for term in ("입력", "타이핑", "작성", "type", "write")) and any(
        marker in folded for marker in ("에 ", "에서 ", "으로 ", "로 ")
    ):
        return True
    if _todo_create_action_from_message(text) is not None:
        return True
    if _todo_delete_action_from_message(text) is not None:
        return True
    if _todo_list_action_from_message(text) is not None:
        return True
    has_object = any(term in folded for term in _ACTION_OBJECT_TERMS)
    has_verb = any(term in folded for term in _ACTION_VERB_TERMS)
    if has_object and has_verb:
        return True
    if _downloads_folder_requested(folded):
        return True
    if any(term in folded for term in ("open ", "launch ", "run ")):
        return True
    return False


def _template_app_open_decision_from_text(
    text: str,
    *,
    context: dict[str, object] | None,
) -> ActionIntentDecision | None:
    if not _context_supports_any_action(context, ("app.open", "app_control")):
        return None

    materialized = materialize_explicit_app_open_for_text(
        text,
        confidence=0.9,
        context=context,
        reason="app_open template matched runtime app metadata",
    )
    decision = _action_decision_from_template_plan(materialized, context=context)
    if decision is not None:
        return decision

    materialized = materialize_fresh_context_app_open_for_text(
        text,
        confidence=0.9,
        context=context,
        reason="fresh context app_open template matched runtime app metadata",
    )
    return _action_decision_from_template_plan(materialized, context=context)


def _template_app_open_type_decision_from_text(
    text: str,
    *,
    context: dict[str, object] | None,
) -> ActionIntentDecision | None:
    if not _context_supports_any_action(context, ("app.open", "app_control")):
        return None
    if not _context_supports_any_action(context, ("keyboard.type", "keyboard_type")):
        return None
    typed_text = _app_type_text_from_message(text)
    if typed_text is None:
        return None

    materialized = materialize_fresh_context_app_open_for_text(
        text,
        confidence=0.9,
        context=context,
        reason="app_open_type template matched runtime app metadata",
    )
    plan = getattr(materialized, "plan", None)
    if plan is None or not getattr(plan, "actions", None):
        return None
    app_action = plan.actions[0]
    app_name = getattr(app_action, "target", None)
    if not isinstance(app_name, str) or not app_name.strip():
        return None

    actions = [
        ClientAction(
            type="app_control",
            command="open",
            target=app_name.strip(),
            args={},
            description=f"Open {app_name.strip()}",
            requires_confirm=False,
        ),
        ClientAction(
            type="keyboard_type",
            command=None,
            target=None,
            payload=typed_text,
            args={"enter": False},
            description="Type text",
            requires_confirm=False,
        ),
    ]
    return ActionIntentDecision(
        should_act=True,
        execution_mode="direct_sequence",
        intent="app.open+keyboard.type",
        confidence=0.9,
        reason="local action template: app open and type",
        actions=actions,
    )


def _app_type_text_from_message(message: str) -> str | None:
    if not any(term in message for term in ("작성", "입력", "타이핑")) and not re.search(
        r"\b(?:type|write|input)\b",
        message,
        flags=re.IGNORECASE,
    ):
        return None
    match = re.search(
        r"(?:에|에서|로|으로|켜서|열어서|열고|켜고)\s*(?P<text>.+?)\s*"
        r"(?:작성|입력|타이핑|type|write|input)"
        r"(?:\s*(?:해|해줘|해줄래|해\s*줄래|해주세요|줘|줄래|부탁해))?"
        r"\s*\??\s*$",
        message,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    text = _normalize_app_type_text(match.group("text"))
    if not text or len(text) > 500:
        return None
    if any(term in text for term in ("소개", "글", "문장", "내용", "대답", "답변")):
        return None
    return text


def _normalize_app_type_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip(" \t\r\n.。!?")
    normalized = re.sub(
        r"\s*(?:라고|이라고|라구|이라구)\s*$",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized.strip(" \t\r\n\"'“”‘’`.,，.。!?")


def _action_decision_from_template_plan(
    materialized,
    *,
    context: dict[str, object] | None,
) -> ActionIntentDecision | None:
    plan = getattr(materialized, "plan", None)
    if plan is None:
        return None
    adapted = V2ToV1ActionAdapter().adapt_plan(plan, context=context)
    if not adapted.valid or not adapted.actions:
        return None
    execution_mode = getattr(plan, "mode", "direct")
    if execution_mode not in DIRECT_EXECUTION_MODES:
        execution_mode = "direct_sequence" if len(adapted.actions) > 1 else "direct"
    plan_actions = getattr(plan, "actions", []) or []
    intent = getattr(plan_actions[0], "name", "action") if plan_actions else "action"
    return ActionIntentDecision(
        should_act=True,
        execution_mode=execution_mode,
        intent=intent,
        confidence=float(getattr(plan, "confidence", 0.9) or 0.9),
        reason=str(
            getattr(plan, "reason", "action template matched")
            or "action template matched"
        ),
        actions=adapted.actions,
        plan=plan,
        validation_errors=[],
    )


def _open_url_from_message(message: str) -> str | None:
    folded = message.casefold()
    if not any(term in folded for term in ("열어", "들어가", "open", "go to", "navigate")):
        return None
    match = re.search(
        r"(?P<url>(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?)",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    url = match.group("url").strip(" \t\r\n,，.。!?")
    if not url:
        return None
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = f"https://{url}"
    return url


def _terminal_action_from_message(
    message: str,
    *,
    context: dict[str, object] | None,
) -> ClientAction | None:
    if not _terminal_enabled(context):
        return None
    command = _terminal_command_from_message(message, context=context)
    if not command:
        return None
    terminal = context.get("terminal") if context else None
    terminal_context = terminal if isinstance(terminal, dict) else {}
    args: dict[str, object] = {"command": command}
    cwd = _terminal_cwd(command, terminal_context)
    if cwd:
        args["cwd"] = cwd
    timeout_seconds = terminal_context.get("timeout_seconds")
    if isinstance(timeout_seconds, int | float) and timeout_seconds > 0:
        args["timeout"] = int(timeout_seconds)
    env = terminal_context.get("env")
    if isinstance(env, dict) and env:
        args["env"] = {
            str(key): str(value)
            for key, value in env.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    shell = terminal_context.get("shell")
    return ClientAction(
        type="terminal",
        command="execute",
        target=shell.strip() if isinstance(shell, str) and shell.strip() else None,
        payload=command,
        args=args,
        description=f"Run terminal command: {command}",
        requires_confirm=True,
    )


def _terminal_command_from_message(
    message: str,
    *,
    context: dict[str, object] | None,
) -> str | None:
    if _looks_like_meta_design_or_analysis_request(message):
        return None
    if _browser_search_query_from_message(message, context=context) is not None:
        return None
    folded = message.casefold()
    terminalish = any(
        term in folded
        for term in (
            "터미널",
            "terminal",
            "cmd",
            "command prompt",
            "콘솔",
            "console",
            "쉘",
            "shell",
            "명령어",
            "command",
        )
    )
    runish = any(
        term in folded
        for term in (
            "실행",
            "수행",
            "쳐",
            "입력",
            "해줘",
            "run",
            "execute",
            "보여",
            "확인",
        )
    )
    if not (terminalish and runish):
        return None

    explicit = _quoted_terminal_command(message)
    if explicit:
        return explicit

    command = _terminal_command_after_marker(message)
    if command:
        return command

    natural = _known_terminal_command_for_message(folded, context=context)
    if natural and _terminal_command_allowed(natural, context):
        return natural
    return None


def _looks_like_meta_design_or_analysis_request(message: str) -> bool:
    folded = message.casefold()
    design_terms = (
        "분석",
        "설계",
        "계획",
        "플랜",
        "리팩토링",
        "리팩터링",
        "구조",
        "아키텍처",
        "우선순위",
        "충돌",
        "비교",
        "제안",
        "고려",
        "analysis",
        "analyze",
        "design",
        "plan",
        "refactor",
        "architecture",
        "priority",
        "conflict",
        "compare",
    )
    action_domain_terms = (
        "액션",
        "라우팅",
        "intent",
        "인텐트",
        "앱 실행",
        "브라우저 검색",
        "터미널 실행",
        "action",
        "routing",
        "browser search",
        "terminal",
    )
    return any(term in folded for term in design_terms) and any(
        term in folded for term in action_domain_terms
    )


def _looks_like_cross_surface_architecture_request(message: str) -> bool:
    folded = message.casefold()
    design_terms = (
        "설계",
        "구조",
        "아키텍처",
        "계획",
        "플랜",
        "제안",
        "고려",
        "나눠서",
        "연동",
        "design",
        "architecture",
        "plan",
        "proposal",
        "integration",
    )
    surface_terms = (
        "백엔드",
        "프론트",
        "프론트엔드",
        "db",
        "데이터베이스",
        "api",
        "todo",
        "할일",
        "캘린더",
        "calendar",
        "연동",
    )
    if not any(term in folded for term in design_terms):
        return False
    return sum(1 for term in surface_terms if term in folded) >= 2


def _looks_like_code_output_request(message: str) -> bool:
    folded = message.casefold()
    code_terms = (
        "코드",
        "소스",
        "함수",
        "클래스",
        "스크립트",
        "프로그램",
        "구현",
        "code",
        "source",
        "function",
        "class",
        "script",
        "program",
        "implementation",
    )
    output_terms = (
        "작성",
        "짜",
        "만들",
        "제공",
        "보여",
        "예시",
        "구현",
        "생성",
        "write",
        "make",
        "create",
        "provide",
        "show",
        "example",
        "generate",
        "implement",
    )
    return any(term in folded for term in code_terms) and any(
        term in folded for term in output_terms
    )


def _obvious_non_realtime_decision(message: str) -> RoutingDecision | None:
    text = message.strip()
    if not text:
        return None
    if _looks_like_code_output_request(text):
        return RoutingDecision(
            mode=ConversationMode.DEEP,
            triggered=True,
            confidence=0.9,
            reasons=["code generation request"],
        )
    if _looks_like_meta_design_or_analysis_request(text):
        return RoutingDecision(
            mode=ConversationMode.DEEP,
            triggered=True,
            confidence=0.95,
            reasons=["analysis-oriented language", "action-routing design request"],
        )
    if _looks_like_cross_surface_architecture_request(text):
        return RoutingDecision(
            mode=ConversationMode.DEEP,
            triggered=True,
            confidence=0.9,
            reasons=["analysis-oriented language", "architecture design request"],
        )
    return None


def _quoted_terminal_command(message: str) -> str | None:
    for pattern in (r"`([^`]+)`", r'"([^"]+)"', r"'([^']+)'"):
        match = re.search(pattern, message)
        if match:
            command = match.group(1).strip()
            if command:
                return command
    return None


def _terminal_command_after_marker(message: str) -> str | None:
    marker = (
        r"(?:터미널(?:에서|로)?|terminal|cmd|command prompt|"
        r"콘솔(?:에서|로)?|console|쉘(?:에서|로)?|shell|명령어|command)"
    )
    command_tail = (
        r"\s*(?:에서|로)?\s*(?P<command>.+?)\s*"
        r"(?:실행(?:해줘|해|시켜줘)?|수행(?:해줘|해)?|쳐줘|입력(?:해줘)?|해줘|run|execute)?\s*$"
    )
    match = re.search(
        marker + command_tail,
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    command = match.group("command").strip(" \t\r\n.。!?")
    command = re.sub(
        r"\s*(?:실행(?:해줘|해|시켜줘)?|수행(?:해줘|해)?|쳐줘|입력(?:해줘)?|해줘|run|execute)\s*$",
        "",
        command,
        flags=re.IGNORECASE,
    ).strip()
    if not command or len(command) > 240:
        return None
    return command


def _known_terminal_command_for_message(
    folded_message: str,
    *,
    context: dict[str, object] | None,
) -> str | None:
    allowed = _terminal_allowed_commands(context)
    normalized_allowed = {
        _terminal_command_root(command): command
        for command in allowed
        if command.strip()
    }
    for command in allowed:
        if command and command.casefold() in folded_message:
            return command
    if (
        "git status" in normalized_allowed
        and "git" in folded_message
        and "status" in folded_message
    ):
        return normalized_allowed["git status"]
    if "pwd" in normalized_allowed and any(
        term in folded_message for term in ("pwd", "현재 위치", "현재경로", "경로")
    ):
        return normalized_allowed["pwd"]
    if "ls" in normalized_allowed and any(
        term in folded_message for term in ("ls", "목록", "리스트", "파일")
    ):
        return normalized_allowed["ls"]
    return None


def _terminal_enabled(context: dict[str, object] | None) -> bool:
    if not context:
        return True
    terminal = context.get("terminal")
    if isinstance(terminal, dict):
        return terminal.get("enabled", True) is not False
    return True


def _terminal_allowed_commands(context: dict[str, object] | None) -> list[str]:
    terminal = context.get("terminal") if context else None
    containers = [terminal, context] if isinstance(terminal, dict) else [context]
    for container in containers:
        if not isinstance(container, dict):
            continue
        value = container.get("allowed_commands")
        if isinstance(value, list):
            result = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if result:
                return result
    return ["echo", "pwd", "ls", "git status"]


def _terminal_command_allowed(
    command: str,
    context: dict[str, object] | None,
) -> bool:
    allowed = _terminal_allowed_commands(context)
    command_key = command.strip().casefold()
    for allowed_command in allowed:
        allowed_key = allowed_command.strip().casefold()
        if not allowed_key:
            continue
        if command_key == allowed_key or command_key.startswith(f"{allowed_key} "):
            return True
    return False


def _terminal_command_root(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip().casefold())


def _terminal_cwd(command: str, terminal_context: dict[str, object]) -> str | None:
    cwd = terminal_context.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        allowed_cwds = terminal_context.get("allowed_cwds")
        if isinstance(allowed_cwds, list) and allowed_cwds:
            allowed = {item for item in allowed_cwds if isinstance(item, str)}
            if cwd not in allowed:
                return None
        return cwd.strip()
    return None


def _local_direct_action_decision(
    message: str,
    *,
    context: dict[str, object] | None,
) -> ActionIntentDecision | None:
    text = message.strip()
    if not text:
        return None
    if _looks_like_meta_design_or_analysis_request(text):
        return None

    app_open_type_decision = _template_app_open_type_decision_from_text(
        text,
        context=context,
    )
    if app_open_type_decision is not None:
        return app_open_type_decision

    app_open_decision = _template_app_open_decision_from_text(text, context=context)
    if app_open_decision is not None:
        return app_open_decision

    result_index = _browser_result_index_from_message(text)
    if result_index is not None and _context_supports_any_action(
        context,
        ("browser.select_result", "browser_control", "browser"),
    ):
        action = ClientAction(
            type="browser_control",
            command="select_result",
            target=None,
            args={"index": result_index},
            description="Open browser search result",
            requires_confirm=False,
        )
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser.select_result",
            confidence=0.9,
            reason="local action template: browser result selection",
            actions=[action],
        )

    folded = text.casefold()
    if _browser_open_only_requested(folded, text) and _context_supports_any_action(
        context,
        ("browser.open", "browser"),
    ):
        action = ClientAction(
            type="browser",
            command="open",
            target=None,
            args={"browser": _default_browser(context)},
            description="Open browser",
            requires_confirm=False,
        )
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser.open",
            confidence=0.9,
            reason="local action template: explicit browser open",
            actions=[action],
        )

    if _browser_close_tab_requested(folded) and _context_supports_any_action(
        context,
        ("browser.close_tab", "browser_control", "browser"),
    ):
        action = ClientAction(
            type="browser_control",
            command="close_tab",
            target="active_tab",
            args={"browser": _default_browser(context)},
            description="Close current browser tab",
            requires_confirm=False,
        )
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser.close_tab",
            confidence=0.9,
            reason="local action template: close current browser tab",
            actions=[action],
        )

    url = _open_url_from_message(text)
    if url and _context_supports_any_action(context, ("open_url", "browser.navigate")):
        action = ClientAction(
            type="open_url",
            command=None,
            target=url,
            args={"browser": _default_browser(context)},
            description="Open URL",
            requires_confirm=False,
        )
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="open_url",
            confidence=0.92,
            reason="local action template: explicit url open",
            actions=[action],
        )

    query = _browser_search_query_from_message(text, context=context)
    if query and _context_supports_any_action(
        context,
        ("open_url", "browser.search", "browser.navigate", "browser"),
    ):
        action = ClientAction(
            type="open_url",
            command=None,
            target=_search_url_for_query(query, context=context),
            args={"browser": _default_browser(context), "query": query},
            description="Search browser",
            requires_confirm=False,
        )
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser.search",
            confidence=0.92,
            reason="local action template: explicit browser search",
            actions=[action],
        )

    terminal_action = _terminal_action_from_message(text, context=context)
    if terminal_action is not None:
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="terminal.run",
            confidence=0.88,
            reason="local action template: terminal command",
            actions=[terminal_action],
        )

    todo_delete_action = _todo_delete_action_from_message(text)
    if todo_delete_action is not None:
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="todo.delete",
            confidence=0.88,
            reason="local action template: todo delete",
            actions=[todo_delete_action],
        )

    todo_action = _todo_create_action_from_message(text)
    if todo_action is not None:
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="todo.create",
            confidence=0.9,
            reason="local action template: todo create",
            actions=[todo_action],
        )

    todo_list_action = _todo_list_action_from_message(text)
    if todo_list_action is not None:
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="todo.list",
            confidence=0.9,
            reason="local action template: todo list",
            actions=[todo_list_action],
        )

    if _browser_open_requested(folded) and _context_supports_any_action(
        context,
        ("browser.open", "browser"),
    ):
        action = ClientAction(
            type="browser",
            command="open",
            target=None,
            args={"browser": _default_browser(context)},
            description="Open browser",
            requires_confirm=False,
        )
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser.open",
            confidence=0.9,
            reason="local action template: explicit browser open",
            actions=[action],
        )

    if _downloads_folder_requested(folded) and _context_supports_any_action(
        context,
        ("file.read", "file_read"),
    ):
        path = _known_folder_path(context, "downloads") or "~/Downloads"
        action = ClientAction(
            type="file_read",
            command=None,
            target=path,
            args={
                "path": path,
                "known_folder": "downloads",
                "mode": "list_directory",
            },
            description="Inspect Downloads folder",
            requires_confirm=False,
        )
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="file.read",
            confidence=0.88,
            reason="local action template: downloads folder inspection",
            actions=[action],
        )

    return None


def _browser_context_active(context: dict[str, object] | None) -> bool:
    if not context:
        return False
    return bool(
        context.get("browser_active")
        or context.get("last_query")
        or context.get("last_url")
    )


def _browser_result_index_from_message(message: str) -> int | None:
    folded = message.casefold()
    if not any(term in folded for term in ("결과", "레시피", "링크", "result", "link")):
        return None
    ordinal_map = {
        "첫": 1,
        "첫번째": 1,
        "1번째": 1,
        "두": 2,
        "두번째": 2,
        "2번째": 2,
        "세": 3,
        "세번째": 3,
        "3번째": 3,
        "fourth": 4,
        "4번째": 4,
        "first": 1,
        "second": 2,
        "third": 3,
    }
    for token, index in ordinal_map.items():
        if token in folded:
            return index
    match = re.search(r"\b([1-9])(?:st|nd|rd|th)?\b", folded)
    if match:
        return int(match.group(1))
    return None


def _browser_search_query_from_message(
    message: str,
    *,
    context: dict[str, object] | None = None,
) -> str | None:
    if _browser_result_index_from_message(message) is not None:
        return None
    folded = message.casefold()
    if _browser_open_only_requested(folded, message):
        return None
    browserish = any(
        term in folded
        for term in ("브라우저", "크롬", "chrome", "browser", "google", "구글", "naver", "네이버")
    )
    searchish = any(term in folded for term in ("검색", "찾아", "search", "find", "look up"))
    explicit_searchish = any(
        term in folded
        for term in ("검색", "서치", "search", "look up", "lookup")
    )
    pageish = any(
        term in folded
        for term in ("페이지", "사이트", "들어가", "열어", "open", "go to", "navigate")
    )
    if not ((browserish and (searchish or pageish)) or explicit_searchish):
        return None
    if _browser_open_requested(folded) and not (
        searchish or _has_query_after_browser_framing(message)
    ):
        return None
    query = normalize_browser_search_query(message)
    if _browser_search_query_is_followup_placeholder(message, query):
        previous_query = _previous_user_message_search_query(context)
        if previous_query:
            return previous_query
        return None
    if _browser_search_query_is_vague(query):
        return None
    if not query or _browser_open_requested(query.casefold()):
        return None
    return query


def _has_query_after_browser_framing(message: str) -> bool:
    query = normalize_browser_search_query(message)
    return bool(query and query.casefold() != message.strip().casefold())


def _browser_search_query_is_followup_placeholder(message: str, query: str) -> bool:
    folded = message.casefold()
    if not any(
        term in folded
        for term in ("브라우저", "크롬", "chrome", "browser", "google", "구글", "naver", "네이버")
    ):
        return False
    reduced = _normalized_action_match_key(query)
    original = _normalized_action_match_key(message)
    return reduced in {
        "",
        "찾아줘",
        "찾아봐",
        "검색해줘",
        "검색해",
        "search",
        "find",
        "lookup",
    } or original in {
        "브라우저에서찾아줘",
        "브라우저에서검색해줘",
        "브라우저로찾아줘",
        "브라우저로검색해줘",
        "크롬에서찾아줘",
        "크롬에서검색해줘",
    }


def _browser_search_query_is_vague(query: str) -> bool:
    return _normalized_action_match_key(query) in {
        "",
        "검색",
        "검색해",
        "검색해줘",
        "검색해봐",
        "검색해줄래",
        "검색해서",
        "서치",
        "줘",
        "주세요",
        "줄래",
        "켜줘",
        "열어줘",
        "열어줄래",
        "search",
        "find",
        "lookup",
    }


def _previous_user_message_search_query(
    context: dict[str, object] | None,
) -> str | None:
    if not context:
        return None
    previous = context.get("previous_user_message")
    text = None
    if isinstance(previous, dict):
        value = previous.get("text")
        if isinstance(value, str):
            text = value
    elif isinstance(previous, str):
        text = previous
    if not text or not text.strip():
        return None
    query = normalize_browser_search_query(text)
    if _browser_search_query_is_followup_placeholder(text, query):
        return None
    return query if query and not _browser_open_requested(query.casefold()) else None


def _local_previous_user_message_response(
    message: str,
    *,
    context: dict[str, object] | None,
) -> str | None:
    if not _previous_user_message_requested(message):
        return None
    previous = _previous_user_message_text(context)
    if previous is None:
        return "기록된 이전 질문이 없습니다."
    return f"이전 질문은 \"{previous}\"였습니다."


def _previous_user_message_requested(message: str) -> bool:
    folded = message.casefold()
    message_key = _normalized_action_match_key(message)
    has_previous_reference = any(
        term in folded
        for term in ("이전", "직전", "방금", "마지막", "last", "previous")
    )
    has_question_reference = any(
        term in folded
        for term in ("질문", "말", "뭐라", "뭐라고", "뭐였", "asked", "said")
    )
    compact_patterns = (
        "내가뭐라",
        "내가뭐라고",
        "뭐라고했",
        "뭐라했",
    )
    return (has_previous_reference and has_question_reference) or any(
        pattern in message_key for pattern in compact_patterns
    )


def _previous_user_message_text(context: dict[str, object] | None) -> str | None:
    if not context:
        return None
    previous = context.get("previous_user_message")
    if isinstance(previous, dict):
        value = previous.get("text")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(previous, str) and previous.strip():
        return previous.strip()
    return None


def _browser_open_requested(folded_message: str) -> bool:
    return any(
        term in folded_message for term in ("브라우저", "크롬", "chrome", "browser")
    ) and any(
        term in folded_message
        for term in ("열어", "켜", "실행", "open", "launch")
    )


def _browser_open_only_requested(folded_message: str, message: str) -> bool:
    if not _browser_open_requested(folded_message):
        return False
    if any(
        term in folded_message
        for term in ("검색", "찾아", "search", "find", "look up", "lookup")
    ):
        return False
    query = normalize_browser_search_query(message)
    return not query or _browser_search_query_is_vague(query)


def _browser_close_tab_requested(folded_message: str) -> bool:
    has_close = any(term in folded_message for term in ("닫", "close"))
    has_tab = any(term in folded_message for term in ("탭", "tab"))
    has_current = any(
        term in folded_message
        for term in ("지금", "현재", "열려있는", "열린", "active", "current")
    )
    has_browser = any(
        term in folded_message for term in ("브라우저", "크롬", "chrome", "browser")
    )
    return has_close and has_tab and (has_current or has_browser)


def _downloads_folder_requested(folded_message: str) -> bool:
    has_downloads = any(term in folded_message for term in ("다운로드", "downloads"))
    has_folder = any(term in folded_message for term in ("폴더", "folder", "파일", "file"))
    has_inspect = any(
        term in folded_message
        for term in ("뭐", "무엇", "있는지", "봐", "보여", "확인", "list", "show")
    )
    return has_downloads and (has_folder or has_inspect)


def _context_supports_any_action(
    context: dict[str, object] | None,
    action_names: tuple[str, ...],
) -> bool:
    if not context:
        return True
    configured: list[str] = []
    for key in ("enabled_capabilities", "capabilities"):
        value = context.get(key)
        if isinstance(value, list):
            configured.extend(item for item in value if isinstance(item, str))
        elif isinstance(value, dict):
            configured.extend(str(key) for key in value.keys())
    if not configured:
        return True
    normalized = {_normalize_action_name(name) for name in configured}
    return any(_normalize_action_name(name) in normalized for name in action_names)


def _normalize_action_name(name: str) -> str:
    return name.strip().casefold().replace("_", ".")


def _default_browser(context: dict[str, object] | None) -> str:
    if context:
        browser = context.get("default_browser")
        if isinstance(browser, str) and browser.strip():
            return browser.strip()
    return "chrome"


def _search_url_for_query(query: str, *, context: dict[str, object] | None) -> str:
    engine = ""
    if context:
        raw_engine = context.get("search_engine")
        if isinstance(raw_engine, str):
            engine = raw_engine.strip().casefold()
    encoded = quote_plus(query)
    if engine == "naver":
        return f"https://search.naver.com/search.naver?query={encoded}"
    return f"https://www.google.com/search?q={encoded}"


def _known_folder_path(context: dict[str, object] | None, key: str) -> str | None:
    if not context:
        return None
    for container_key in ("known_folders", "folders", "filesystem"):
        value = context.get(container_key)
        if isinstance(value, dict):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            nested = value.get("known_folders")
            if isinstance(nested, dict):
                candidate = nested.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return None


def _resolve_client_action_decision(
    message: str,
    *,
    request: Request | None,
    user_id: str | None,
    context: dict[str, object] | None = None,
) -> ActionIntentDecision | None:
    return _client_action_decision(
        message,
        request=request,
        user_id=user_id,
        context=context,
    )


def _start_action_decision_future(
    message: str,
    *,
    request: Request,
    user_id: str,
    context: dict[str, object] | None = None,
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
        context=context,
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
    action_candidate = _looks_like_direct_client_action_request(
        message,
        context=context,
    )

    def done_wait_seconds() -> float:
        grace = _action_intent_done_grace_seconds()
        if not action_candidate:
            return grace
        return max(grace, _action_candidate_wait_seconds())

    def ready_decision_chunks(*, timeout: float = 0.0) -> tuple[list[bytes], bool]:
        nonlocal emitted_action_intent
        if action_future is None or emitted_action_intent:
            return [], False
        if not action_future.done():
            if timeout <= 0:
                return [], False
            try:
                decision = action_future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                return [], False
            except Exception as exc:
                logger.warning("action intent decision failed after stream start: %s", exc)
                decision = None
        else:
            try:
                decision = action_future.result()
            except Exception as exc:
                logger.warning("action intent decision failed after stream start: %s", exc)
                decision = None

        emitted_action_intent = True
        chunks = [
            _sse_event(
                "action_intent",
                _action_intent_payload(decision, unavailable=decision is None),
            )
        ]
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
            return chunks, True
        if _is_unavailable_action_decision(decision):
            chunks.extend(
                _stream_unavailable_action_decision(
                    decision=decision,
                    request_id=request_id,
                )
            )
            return chunks, True
        return chunks, False

    try:
        if action_candidate:
            yield _sse_event(
                "action_intent",
                {
                    "should_act": True,
                    "execution_mode": "pending",
                    "intent": None,
                    "confidence": 0.0,
                    "reason": "action gate running",
                    "stage": "gate",
                    "status": "in_progress",
                    "action_count": 0,
                },
            )
        for chunk in _stream_with_embedded_action_intercept(
            stream,
            request_id=request_id,
            message=message,
            user_id=user_id,
            action_dispatcher=action_dispatcher,
            context=context,
        ):
            if _is_embedded_action_handling_chunk(chunk):
                emitted_action_intent = True
                yield chunk
                continue

            if _is_action_ack_delta_chunk(chunk):
                decision_chunks, stopped = ready_decision_chunks()
                for decision_chunk in decision_chunks:
                    yield decision_chunk
                if stopped:
                    return
                continue

            if _is_assistant_done_chunk(chunk):
                if _is_action_ack_done_chunk(chunk):
                    decision_chunks, stopped = ready_decision_chunks(
                        timeout=_action_ack_recovery_grace_seconds(),
                    )
                    for decision_chunk in decision_chunks:
                        yield decision_chunk
                    if stopped:
                        return
                    if action_future is not None and not emitted_action_intent:
                        emitted_action_intent = True
                        yield _sse_event(
                            "action_intent",
                            _action_intent_payload(None, unavailable=True),
                        )
                    yield _action_ack_suppressed_done()
                    return

                decision_chunks, stopped = ready_decision_chunks(
                    timeout=done_wait_seconds(),
                )
                for decision_chunk in decision_chunks:
                    yield decision_chunk
                if stopped:
                    return
                yield chunk
                continue

            decision_chunks, stopped = ready_decision_chunks()
            for decision_chunk in decision_chunks:
                yield decision_chunk
            if stopped:
                return
            yield chunk

        decision_chunks, stopped = ready_decision_chunks(
            timeout=done_wait_seconds(),
        )
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
    embedded_validation_errors = []
    saw_action_block = False
    text_plan_step_id = f"{request_id}:text"
    emitted_text_plan_step = False
    completed_text_plan_step = False

    def emit_text_plan_step(status: str) -> bytes:
        return _sse_event(
            "plan_step",
            {
                "id": text_plan_step_id,
                "title": "텍스트 응답 작성",
                "description": "요청에 대한 답변을 생성 중입니다.",
                "status": status,
            },
        )

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
            elif event_name == "assistant_delta":
                content = payload.get("content", "")
                if (
                    isinstance(content, str)
                    and content
                    and not emitted_text_plan_step
                    and content.strip() != _ACTION_ACK.strip()
                ):
                    emitted_text_plan_step = True
                    yield emit_text_plan_step("in_progress")

            elif event_name in {"assistant_done", "conversation.done", "done"}:
                content = payload.get("content") or payload.get("text")
                if isinstance(content, str):
                    saw_action_block = _contains_action_block(content)
                    embedded_result = parse_embedded_actions_from_text(
                        content,
                        context=context,
                    )
                    embedded_validation_errors = embedded_result.issues
                    saw_action_block = saw_action_block or embedded_result.saw_action_block
                    suppress_current_chunk = saw_action_block
                if emitted_text_plan_step and not completed_text_plan_step:
                    completed_text_plan_step = True
                    yield emit_text_plan_step("completed")

            event_name = None
            data_lines = []

        if suppress_current_chunk:
            break
        yield chunk

    if emitted_text_plan_step and not completed_text_plan_step:
        yield emit_text_plan_step("completed")

    if saw_action_block:
        yield _sse_event(
            "action_compile_retry",
            {
                "request_id": request_id,
                "status": "retrying_compile",
                "reason": "embedded assistant action text is not an executable action source",
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
                "status": "suppressed",
                "failure_reason": _embedded_failure_reason(
                    embedded_validation_errors,
                    retry_decision,
                ),
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
        or (
            "```bash" in lowered
            and (
                "\nopen " in lowered
                or "\nopen\t" in lowered
                or "\necho " in lowered
                or "\npython " in lowered
                or "\npython3 " in lowered
                or "\nosascript " in lowered
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


def _embedded_failure_reason(
    validation_errors: list[object],
    retry_decision: ActionIntentDecision | None,
) -> str:
    if retry_decision is None:
        return "compiler_unavailable"
    if getattr(retry_decision, "validation_errors", None) or validation_errors:
        return "backend_validation_failed"
    return "execution_failed"


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
        context["capabilities"] = enabled_capabilities
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
    working_context = (
        store.working_context(user_id)
        if store is not None and hasattr(store, "working_context")
        else None
    )
    if working_context is not None:
        context["working_context"] = working_context
    previous_user_message = _previous_user_message_context(
        request=request,
        user_id=user_id,
    )
    if previous_user_message is not None:
        context["previous_user_message"] = previous_user_message
    return context or None


def _previous_user_message_context(
    *,
    request: Request,
    user_id: str,
) -> dict[str, object] | None:
    memory = _user_message_memory(request)
    record = memory.get(user_id)
    if not isinstance(record, dict):
        return None
    updated_at = record.get("updated_at")
    text = record.get("text")
    if not isinstance(updated_at, int | float):
        return None
    if time.monotonic() - float(updated_at) > _RECENT_USER_MESSAGE_TTL_SECONDS:
        memory.pop(user_id, None)
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return {"text": text.strip()}


def _record_user_message_context(
    *,
    request: Request,
    user_id: str,
    message: str,
) -> None:
    text = message.strip()
    if not text:
        return
    _user_message_memory(request)[user_id] = {
        "text": text,
        "updated_at": time.monotonic(),
    }


def _user_message_memory(request: Request) -> dict[str, dict[str, object]]:
    memory = getattr(request.app.state, "recent_user_messages", None)
    if isinstance(memory, dict):
        return memory
    request.app.state.recent_user_messages = {}
    return request.app.state.recent_user_messages


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
    for key in ("action_id", "action_type", "command", "target", "status", "output"):
        item = getattr(value, key, None)
        if item is not None:
            payload[key] = item
    output = payload.get("output")
    if payload.get("action_type") == "app_control" and isinstance(output, dict):
        app_name = (
            _string_from_mapping(output, "active_app")
            or _string_from_mapping(output, "launched_app")
            or _string_from_mapping(output, "app")
            or _string_from_mapping(payload, "target")
        )
        if app_name:
            payload["app"] = app_name
            payload["active_app"] = _string_from_mapping(output, "active_app") or app_name
            if payload.get("command") == "open":
                payload["launched_app"] = (
                    _string_from_mapping(output, "launched_app") or app_name
                )
        for key in ("bundle_id", "source"):
            item = _string_from_mapping(output, key)
            if item:
                payload[key] = item
    return payload


def _string_from_mapping(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _trim_action_context_for_message(
    context: dict[str, object] | None,
    message: str,
) -> dict[str, object] | None:
    if not context:
        return context
    applications = context.get("available_applications")
    names = context.get("available_application_names") or applications
    if not isinstance(names, list) or len(names) <= 30:
        return context

    message_key = message.casefold()
    matched_names: list[str] = []
    matched_apps: list[dict[str, object]] = []
    if (
        isinstance(applications, list)
        and applications
        and isinstance(applications[0], dict)
    ):
        for app in applications:
            if not isinstance(app, dict):
                continue
            name = app.get("name")
            candidates = [name] if isinstance(name, str) else []
            for key in ("display_name", "aliases", "capabilities", "categories", "keywords"):
                value = app.get(key)
                if isinstance(value, str):
                    candidates.append(value)
                elif isinstance(value, list):
                    candidates.extend(item for item in value if isinstance(item, str))
            if any(candidate.strip().casefold() in message_key for candidate in candidates):
                matched_apps.append(app)
                if isinstance(name, str):
                    matched_names.append(name)
    else:
        matched_names = []
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            candidates = [name, *_local_app_aliases_for_name(name)]
            if any(candidate.strip().casefold() in message_key for candidate in candidates):
                matched_names.append(name)
    trimmed = dict(context)
    if matched_apps:
        trimmed["available_applications"] = matched_apps[:30]
        trimmed["available_application_names"] = matched_names[:30]
    elif matched_names:
        trimmed["available_applications"] = matched_names[:30]
        trimmed["available_application_names"] = matched_names[:30]
    else:
        trimmed["available_application_names"] = [
            name
            for name in names[:_action_context_trimmed_application_name_limit()]
            if isinstance(name, str) and name.strip()
        ]
        trimmed.pop("available_applications", None)
    return trimmed


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

    profile_capabilities = profile.get("enabled_capabilities") or profile.get("capabilities")
    if isinstance(profile_capabilities, list | dict):
        context["capabilities"] = _merge_capability_context(
            context.get("capabilities"),
            profile_capabilities,
        )

    applications = _runtime_applications_for_context(profile.get("applications"))
    if applications:
        application_names = [
            app["name"] for app in applications if isinstance(app.get("name"), str)
        ]
        context["available_applications"] = applications
        context["available_application_names"] = application_names

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
                "env",
                "allowed_commands",
                "allowed_cwds",
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
    for item in value[:_action_context_application_limit()]:
        if isinstance(item, str):
            name = item.strip()
            if not name:
                continue
            app = {"name": name}
            _enrich_runtime_application_aliases(app)
            applications.append(app)
            continue
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
        for key in ("aliases", "capabilities", "categories", "keywords"):
            values = _string_list(item.get(key))
            if values:
                app[key] = values[:12]
        _enrich_runtime_application_aliases(app)
        applications.append(app)
    return applications


def _enrich_runtime_application_aliases(app: dict[str, object]) -> None:
    profile = _local_app_alias_profile_for_app(app)
    if not profile:
        return
    for key, values in profile.items():
        merged = _string_list(app.get(key))
        merged.extend(values)
        app[key] = list(dict.fromkeys(merged))[:12]


def _local_app_aliases_for_name(app_name: str) -> list[str]:
    app_key = _normalized_action_match_key(app_name)
    if not app_key:
        return []
    for bundle_id, profile in _LOCAL_APP_ALIAS_PROFILE.items():
        bundle_key = _normalized_action_match_key(bundle_id)
        candidates = []
        for values in profile.values():
            candidates.extend(values)
        candidate_keys = {_normalized_action_match_key(candidate) for candidate in candidates}
        if app_key == bundle_key or app_key in candidate_keys:
            return list(dict.fromkeys(candidates))
    return []


def _local_app_alias_profile_for_app(
    app: dict[str, object],
) -> dict[str, tuple[str, ...]] | None:
    identity_values = [
        value
        for key in ("bundle_id", "name", "display_name", "executable")
        for value in [app.get(key)]
        if isinstance(value, str) and value.strip()
    ]
    identity_keys = {_normalized_action_match_key(value) for value in identity_values}
    for bundle_id, profile in _LOCAL_APP_ALIAS_PROFILE.items():
        bundle_key = _normalized_action_match_key(bundle_id)
        aliases = profile.get("aliases", ())
        alias_keys = {_normalized_action_match_key(alias) for alias in aliases}
        if bundle_key in identity_keys or identity_keys.intersection(alias_keys):
            return profile
    return None


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
    context: dict[str, object] | None = None,
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
    context = _trim_action_context_for_message(
        context
        if context is not None
        else _client_action_context(request=request, user_id=user_id),
        message,
    )
    local_decision = _local_direct_action_decision(message, context=context)
    if local_decision is not None:
        return local_decision
    latest_observation = _latest_observation_context(request=request, user_id=user_id)
    decision = classify_client_action_intent_decision(
        message,
        context=context,
        latest_observation=latest_observation,
        validation_errors=validation_errors,  # type: ignore[arg-type]
    )
    if decision is not None:
        fallback_decision = _local_direct_action_decision(message, context=context)
        if not _is_direct_action_decision(decision) and fallback_decision is not None:
            return fallback_decision
        return decision
    core_decision = _client_action_decision_via_core_model(
        message,
        request=request,
        user_id=user_id,
        context=context,
        validation_errors=validation_errors,
    )
    if core_decision is not None:
        fallback_decision = _local_direct_action_decision(message, context=context)
        if not _is_direct_action_decision(core_decision) and fallback_decision is not None:
            return fallback_decision
        return core_decision
    return None


def _client_action_decision_via_core_model(
    message: str,
    *,
    request: Request | None,
    user_id: str | None,
    context: dict[str, object] | None,
    validation_errors: list[object] | None = None,
) -> ActionIntentDecision | None:
    if not _action_intent_core_fallback_enabled():
        return None
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
            "failure_reason": "compiler_unavailable" if unavailable else None,
            "action_count": 0,
            "stage": "failed",
            "status": "failed",
        }
    payload: dict[str, object] = {
        "should_act": decision.should_act,
        "execution_mode": decision.execution_mode,
        "intent": decision.intent,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "action_count": len(decision.actions),
        "stage": "failed" if decision.execution_mode == "invalid" else ("planning" if decision.should_act else "complete"),
        "status": "failed" if decision.execution_mode == "invalid" else "complete",
    }
    validation_errors = [
        issue.model_dump()
        for issue in (decision.validation_errors or [])
        if hasattr(issue, "model_dump")
    ]
    if validation_errors:
        payload["failure_reason"] = "backend_validation_failed"
        payload["validation_errors"] = validation_errors
    elif decision.execution_mode == "invalid":
        payload["failure_reason"] = "action_unavailable"
    return payload


def _is_direct_action_decision(decision: ActionIntentDecision | None) -> bool:
    return (
        decision is not None
        and decision.execution_mode in DIRECT_EXECUTION_MODES
        and bool(decision.actions)
    )


def _is_unavailable_action_decision(decision: ActionIntentDecision | None) -> bool:
    if decision is None:
        return False
    if decision.execution_mode == "invalid":
        return True
    return (
        decision.should_act
        and not decision.actions
        and decision.reason in {
            "action gate lacked a supported template",
            "ungrounded app action",
        }
    )


def _stream_unavailable_action_decision(
    *,
    decision: ActionIntentDecision,
    request_id: str,
) -> Generator[bytes, None, None]:
    reason = _action_decision_reason(decision)
    yield _sse_event(
        "plan_step",
        {
            "id": f"{request_id}:action-unavailable",
            "title": "액션 실행 불가",
            "description": reason,
            "status": "failed",
        },
    )
    yield _sse_event(
        "assistant_done",
        {
            "content": f"요청은 작업으로 인식했지만 실행할 수 없습니다. {reason}",
            "summary": "client action unavailable",
            "status": "failed",
            "failure_reason": "action_unavailable",
            "has_actions": False,
            "action_count": 0,
            "action_results": [],
            "error": reason,
        },
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
    decision_step_id = f"{request_id}:decision"
    execute_step_id = f"{request_id}:action-start"
    yield _sse_event(
        "plan_step",
        {
            "id": decision_step_id,
            "title": "요청 판별",
            "description": "요청을 작업으로 분류했습니다.",
            "status": "in_progress",
        },
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
        "plan_step",
        {
            "id": decision_step_id,
            "title": "요청 판별",
            "description": "요청을 작업으로 분류했습니다.",
            "status": "completed",
        },
    )
    first_action = decision.actions[0] if decision.actions else None
    first_title = (
        first_action.description
        if first_action and first_action.description
        else (
            f"{first_action.type}/{first_action.command}"
            if first_action and first_action.command
            else (first_action.type if first_action else "액션")
        )
    )
    yield _sse_event(
        "plan_step",
        {
            "id": execute_step_id,
            "title": "액션 실행 시작",
            "description": first_title,
            "status": "in_progress",
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
    yield _sse_event(
        "plan_step",
        {
            "id": execute_step_id,
            "title": "액션 실행 시작",
            "description": first_title,
            "status": "completed",
        },
    )


def _stream_local_text_response(
    *,
    request_id: str,
    content: str,
    confidence: float = 0.98,
    reason: str = "local conversation context",
) -> Generator[bytes, None, None]:
    step_id = f"{request_id}:text"
    yield _sse_event(
        "classification",
        {
            "category": "general",
            "mode": ConversationMode.REALTIME.value,
            "confidence": confidence,
            "reasons": [reason],
        },
    )
    yield _sse_event(
        "plan_step",
        {
            "id": step_id,
            "title": "텍스트 응답 작성",
            "description": "요청에 대한 답변을 생성 중입니다.",
            "status": "in_progress",
        },
    )
    yield _sse_event("assistant_delta", {"content": content})
    yield _sse_event(
        "plan_step",
        {
            "id": step_id,
            "title": "텍스트 응답 작성",
            "description": "요청에 대한 답변을 생성 중입니다.",
            "status": "completed",
        },
    )
    yield _sse_event(
        "assistant_done",
        {
            "content": content,
            "summary": reason,
        },
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
    return _dispatch_actions_sync(
        actions=actions,
        request_id=request_id,
        user_id=user_id,
        action_dispatcher=action_dispatcher,
        execution_context=execution_context,
    )


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
    yield from _stream_action_dispatch_events(
        actions=actions,
        request_id=request_id,
        user_id=user_id,
        action_dispatcher=action_dispatcher,
        action_results=action_results,
        all_actions=all_actions,
        execution_context=execution_context,
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


def _turn_cancellation_store(request: Request):
    store = getattr(request.app.state, "turn_cancellation", None)
    if store is None:
        from planner.turn_cancellation import TurnCancellationStore

        store = TurnCancellationStore()
        request.app.state.turn_cancellation = store
    return store


def _cancel_pending_actions_for_turn(
    *,
    request: Request,
    user_id: str,
    request_id: str | None,
    reason: str,
) -> int:
    if not request_id:
        return 0
    dispatcher = getattr(request.app.state, "action_dispatcher", None)
    if dispatcher is None or not hasattr(dispatcher, "cancel_request"):
        return 0
    return dispatcher.cancel_request(
        user_id=user_id,
        request_id=request_id,
        reason=reason,
    )


def _conversation_cancelled_chunk(
    *,
    request_id: str,
    reason: str,
) -> bytes:
    return _sse_event(
        "conversation.cancelled",
        {
            "request_id": request_id,
            "reason": reason,
        },
    )


def _stream_orchestrated_conversation(
    req: ConversationRequest,
    request: Request,
    principal,
) -> Generator[bytes, None, None]:
    request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"
    turn_store = _turn_cancellation_store(request)
    previous_request_id = turn_store.begin_turn(
        user_id=principal.user_id,
        request_id=request_id,
        reason="barge_in",
    )
    _cancel_pending_actions_for_turn(
        request=request,
        user_id=principal.user_id,
        request_id=previous_request_id,
        reason="barge_in",
    )
    try:
        inner_stream = _stream_orchestrated_conversation_inner(
            req,
            request,
            principal,
            request_id=request_id,
        )
        response_stream = _stream_with_live_tts(
            inner_stream,
            req=req,
            request_id=request_id,
            user_id=principal.user_id,
        )
        for chunk in response_stream:
            cancellation = turn_store.cancellation(
                user_id=principal.user_id,
                request_id=request_id,
            )
            if cancellation is not None:
                yield _conversation_cancelled_chunk(
                    request_id=request_id,
                    reason=cancellation.reason,
                )
                return
            yield chunk
    finally:
        turn_store.finish_turn(user_id=principal.user_id, request_id=request_id)


def _stream_orchestrated_conversation_inner(
    req: ConversationRequest,
    request: Request,
    principal,
    *,
    request_id: str,
) -> Generator[bytes, None, None]:

    action_executor: concurrent.futures.ThreadPoolExecutor | None = None
    action_future: concurrent.futures.Future[ActionIntentDecision | None] | None = None
    route_executor: concurrent.futures.ThreadPoolExecutor | None = None
    route_future: concurrent.futures.Future[RoutingDecision] | None = None
    client_action_context = _client_action_context(
        request=request,
        user_id=principal.user_id,
    )
    trimmed_action_context = _trim_action_context_for_message(
        client_action_context,
        req.message,
    )
    local_text_response = _local_previous_user_message_response(
        req.message,
        context=trimmed_action_context,
    )
    _record_user_message_context(
        request=request,
        user_id=principal.user_id,
        message=req.message,
    )
    routing_context = ConversationContext(
        recent_failures=req.recent_failures,
        ambiguity_count=req.ambiguity_count,
        turn_index=req.turn_index,
    )
    route_override = req.override.value if req.override else None
    obvious_decision: RoutingDecision | None = None

    if route_override in (None, ConversationMode.REALTIME.value):
        autonomous_goal = autonomous_loop_requested(req.message)
        if autonomous_goal is not None:
            _start_background_autonomous_loop(
                request=request,
                user_id=principal.user_id,
                goal=autonomous_goal,
            )
            yield from _stream_local_text_response(
                request_id=request_id,
                content="알겠습니다, 계속 지켜보면서 진행할게요. 다 되면 알려드릴게요.",
                reason="autonomous loop started in background",
            )
            return

        if local_text_response is not None and req.override != ContractConversationMode.PLANNING:
            yield from _stream_local_text_response(
                request_id=request_id,
                content=local_text_response,
                reason="local previous user message recall",
            )
            return

        if req.override != ContractConversationMode.PLANNING:
            local_decision = _local_direct_action_decision(
                req.message,
                context=trimmed_action_context,
            )
            if _is_direct_action_decision(local_decision):
                yield _sse_event(
                    "action_intent",
                    _action_intent_payload(local_decision),
                )
                yield from _stream_direct_action_decision(
                    decision=local_decision,
                    message=req.message,
                    request_id=request_id,
                    user_id=principal.user_id,
                    action_dispatcher=request.app.state.action_dispatcher,
                )
                return

            if route_override is None:
                obvious_decision = _obvious_non_realtime_decision(req.message)

            if obvious_decision is None and _looks_like_direct_client_action_request(
                req.message,
                context=trimmed_action_context,
            ):
                action_executor, action_future = _start_action_decision_future(
                    req.message,
                    request=request,
                    user_id=principal.user_id,
                    context=trimmed_action_context,
                )

        if obvious_decision is None:
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
                    context=client_action_context,
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

    decision = obvious_decision or evaluate_conversation_mode(
        req.message,
        override=route_override,
        context=routing_context,
    )
    _log_classification(req.message, decision.mode, decision.confidence)

    for chunk in _classification_chunks_for_decision(decision):
        yield chunk

    if obvious_decision is not None and req.override is None:
        stream = request.app.state.core_client.chat_stream(
            message=req.message,
            task_type="analysis",
            confirm=False,
            route_override="deep",
            user_id=principal.user_id,
            user_email=getattr(principal, "email", ""),
            request_id=request_id,
        )
        for chunk in _stream_without_leading_action_ack(stream):
            yield chunk
        return

    action_decision: ActionIntentDecision | None = None
    if (
        req.override != ContractConversationMode.PLANNING
        and action_future is None
        and _looks_like_direct_client_action_request(
            req.message,
            context=trimmed_action_context,
        )
    ):
        action_executor, action_future = _start_action_decision_future(
            req.message,
            request=request,
            user_id=principal.user_id,
            context=trimmed_action_context,
        )
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

        if _is_unavailable_action_decision(action_decision):
            yield from _stream_unavailable_action_decision(
                decision=action_decision,
                request_id=request_id,
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
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "jarvis-controller",
        "action_runtime": _action_runtime_config_payload(),
    }


@api_router.get("/tts-test", tags=["health"], summary="Browser TTS test page")
def tts_test_page() -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>JARVIS TTS Test</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f1ea;
      --ink: #1f2522;
      --muted: #65716c;
      --line: #d7d0c3;
      --panel: #fffaf1;
      --accent: #0f766e;
      --accent-dark: #0b5e58;
      --bad: #b42318;
      --good: #067647;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(135deg, rgba(15,118,110,.12), transparent 34%),
        linear-gradient(225deg, rgba(173,107,34,.12), transparent 38%),
        var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
    }
    main { width: min(1120px, calc(100% - 32px)); margin: 32px auto; }
    h1 { margin: 0 0 6px; font-size: clamp(28px, 4vw, 48px); letter-spacing: 0; }
    p { margin: 0; color: var(--muted); }
    .grid { display: grid; grid-template-columns: 360px 1fr; gap: 18px; margin-top: 24px; }
    section {
      background: color-mix(in srgb, var(--panel) 92%, white);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 12px 35px rgba(40, 35, 28, .08);
    }
    label { display: grid; gap: 6px; margin: 12px 0; font-weight: 700; }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font: inherit;
      background: #fffef9;
      color: var(--ink);
    }
    textarea { min-height: 128px; resize: vertical; line-height: 1.55; }
    button {
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      font: inherit;
      font-weight: 800;
      color: white;
      background: var(--accent);
      cursor: pointer;
    }
    button:hover { background: var(--accent-dark); }
    button.secondary { background: #3d4b46; }
    button.stop { background: var(--bad); }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .metric {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin: 14px 0;
    }
    .metric div { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fffef9; }
    .metric strong { display: block; font-size: 20px; }
    pre {
      min-height: 220px;
      max-height: 380px;
      overflow: auto;
      white-space: pre-wrap;
      background: #111816;
      color: #d9f2e6;
      border-radius: 8px;
      padding: 14px;
    }
    @media (max-width: 820px) { .grid { grid-template-columns: 1fr; } .metric { grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
<main>
  <h1>JARVIS TTS Test</h1>
  <p>직접 PCM TTS와 대화 병렬 TTS의 지연 시간을 브라우저에서 비교합니다.</p>
  <div class="grid">
    <section>
      <label>Bearer token <input id="token" type="password" placeholder="Authorization token" /></label>
      <label>Voice <input id="voice" value="default" /></label>
      <label>Model <input id="model" placeholder="비우면 서버 기본값" /></label>
      <label>Sample rate <select id="sampleRate"><option>24000</option><option>16000</option><option>48000</option></select></label>
      <div class="row">
        <button id="directBtn">직접 TTS 테스트</button>
        <button id="liveBtn" class="secondary">대화+라이브 TTS</button>
        <button id="stopBtn" class="stop">중지</button>
      </div>
    </section>
    <section>
      <label>Text / Message
        <textarea id="text">안녕하세요. 지금 실시간 한국어 음성 합성 속도를 테스트하고 있습니다. 첫 오디오가 나오는 시간과 전체 생성 시간을 확인합니다.</textarea>
      </label>
      <div class="metric">
        <div><span>TTFB</span><strong id="ttfb">-</strong></div>
        <div><span>Audio bytes</span><strong id="bytes">0</strong></div>
        <div><span>Total</span><strong id="total">-</strong></div>
        <div><span>Status</span><strong id="status">idle</strong></div>
      </div>
      <pre id="log"></pre>
    </section>
  </div>
</main>
<script>
let aborter = null;
let audioContext = null;
let nextPlayTime = 0;

const $ = (id) => document.getElementById(id);
const log = (msg) => {
  const now = new Date().toLocaleTimeString();
  $("log").textContent += `[${now}] ${msg}\\n`;
  $("log").scrollTop = $("log").scrollHeight;
};
const authHeaders = () => {
  const token = $("token").value.trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
};
const resetMetrics = () => {
  $("ttfb").textContent = "-";
  $("bytes").textContent = "0";
  $("total").textContent = "-";
  $("status").textContent = "running";
  $("log").textContent = "";
};
const ttsConfig = () => {
  const model = $("model").value.trim();
  const body = {
    voice: $("voice").value.trim() || "default",
    sample_rate: Number($("sampleRate").value),
    channels: 1,
    sample_width: 2,
    format: "pcm_s16le",
  };
  if (model) body.model = model;
  return body;
};
const ensureAudio = async (sampleRate) => {
  if (!audioContext || audioContext.sampleRate !== sampleRate) {
    audioContext = new AudioContext({ sampleRate });
    nextPlayTime = audioContext.currentTime;
  }
  if (audioContext.state === "suspended") await audioContext.resume();
};
const playPcm16 = async (chunk, sampleRate) => {
  await ensureAudio(sampleRate);
  const view = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  const frames = Math.floor(chunk.byteLength / 2);
  const audioBuffer = audioContext.createBuffer(1, frames, sampleRate);
  const data = audioBuffer.getChannelData(0);
  for (let i = 0; i < frames; i++) data[i] = view.getInt16(i * 2, true) / 32768;
  const source = audioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioContext.destination);
  nextPlayTime = Math.max(nextPlayTime, audioContext.currentTime + 0.03);
  source.start(nextPlayTime);
  nextPlayTime += audioBuffer.duration;
};
const readAudioStream = async (response, startedAt, sampleRate) => {
  if (!response.ok || !response.body) throw new Error(`audio ${response.status}`);
  const reader = response.body.getReader();
  let first = true;
  let totalBytes = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    if (first) {
      first = false;
      $("ttfb").textContent = `${Math.round(performance.now() - startedAt)}ms`;
      log("first audio chunk received");
    }
    totalBytes += value.byteLength;
    $("bytes").textContent = String(totalBytes);
    await playPcm16(value, sampleRate);
  }
  $("total").textContent = `${Math.round(performance.now() - startedAt)}ms`;
  $("status").textContent = "done";
};
const parseSse = (buffer) => {
  const events = [];
  const parts = buffer.split("\\n\\n");
  const rest = parts.pop();
  for (const part of parts) {
    let event = "message";
    const data = [];
    for (const line of part.split("\\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (data.length) events.push({ event, data: data.join("\\n") });
  }
  return { events, rest };
};
const directTts = async () => {
  aborter = new AbortController();
  resetMetrics();
  const startedAt = performance.now();
  const sampleRate = Number($("sampleRate").value);
  const body = { ...ttsConfig(), chunks: [{ id: "browser-test", text: $("text").value }] };
  log("POST /audio/speech/pcm");
  const res = await fetch("/audio/speech/pcm", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
    signal: aborter.signal,
  });
  await readAudioStream(res, startedAt, sampleRate);
};
const liveTts = async () => {
  aborter = new AbortController();
  resetMetrics();
  const startedAt = performance.now();
  const body = {
    message: $("text").value,
    tts_enabled: true,
    tts_voice: $("voice").value.trim() || "default",
    tts_model: $("model").value.trim() || null,
    tts_sample_rate: Number($("sampleRate").value),
    tts_channels: 1,
    tts_sample_width: 2,
  };
  log("POST /conversation/stream");
  const res = await fetch("/conversation/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
    signal: aborter.signal,
  });
  if (!res.ok || !res.body) throw new Error(`conversation ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSse(buffer);
    buffer = parsed.rest;
    for (const item of parsed.events) {
      if (item.event === "assistant_delta") {
        const payload = JSON.parse(item.data);
        if (payload.content) log(`text: ${payload.content}`);
      }
      if (item.event === "tts_session") {
        const payload = JSON.parse(item.data);
        log(`GET ${payload.stream_url}`);
        readAudioStream(
          await fetch(payload.stream_url, { headers: authHeaders(), signal: aborter.signal }),
          startedAt,
          payload.sample_rate
        ).catch((error) => {
          $("status").textContent = "error";
          log(error.message);
        });
      }
    }
  }
};
$("directBtn").onclick = () => directTts().catch((error) => { $("status").textContent = "error"; log(error.message); });
$("liveBtn").onclick = () => liveTts().catch((error) => { $("status").textContent = "error"; log(error.message); });
$("stopBtn").onclick = () => { if (aborter) aborter.abort(); $("status").textContent = "stopped"; };
</script>
</body>
</html>
        """.strip()
    )


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
    "/conversation/cancel",
    tags=["conversation"],
    summary="Cancel the active or specified conversation turn",
)
def conversation_cancel(
    req: ConversationCancelRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> dict[str, object]:
    _ = authorization_header
    principal = request.state.principal
    reason = req.reason.strip() or "barge_in"
    turn_store = _turn_cancellation_store(request)
    cancelled_request_id = turn_store.cancel_turn(
        user_id=principal.user_id,
        request_id=req.request_id,
        reason=reason,
    )
    cancelled_actions = _cancel_pending_actions_for_turn(
        request=request,
        user_id=principal.user_id,
        request_id=cancelled_request_id,
        reason=reason,
    )
    return {
        "cancelled": cancelled_request_id is not None,
        "request_id": cancelled_request_id,
        "reason": reason,
        "cancelled_actions": cancelled_actions,
    }


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
                response_stream = _stream_realtime_with_action_arbitration(
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
                response_stream = _stream_with_model_logging(
                    stream,
                    request_id=request_id,
                    message=req.message,
                )
            yield from _stream_with_live_tts(
                response_stream,
                req=req,
                request_id=request_id,
                user_id=principal.user_id,
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
        response_stream = _stream_realtime_with_parallel_decisions(
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
        yield from _stream_with_live_tts(
            response_stream,
            req=req,
            request_id=request_id,
            user_id=principal.user_id,
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


# ── audio ───────────────────────────────────────────────────


@api_router.post("/audio/speech", tags=["audio"], summary="Synthesize speech")
def synthesize_speech(
    req: TextToSpeechRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> Response:
    _ = authorization_header
    principal = request.state.principal
    request_id = request.headers.get("x-request-id", "")
    result = request.app.state.core_client.synthesize_speech(
        user_id=principal.user_id,
        body=req.model_dump(exclude_none=True),
        request_id=request_id,
    )
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers=result.headers,
    )


@api_router.post("/audio/speech/pcm", tags=["audio"], summary="Stream speech PCM")
def synthesize_speech_pcm(
    req: TextToSpeechPCMRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> StreamingResponse:
    _ = authorization_header
    principal = request.state.principal
    request_id = request.headers.get("x-request-id", "")
    result = request.app.state.core_client.synthesize_speech_pcm_stream(
        user_id=principal.user_id,
        body=req.model_dump(exclude_none=True),
        request_id=request_id,
    )
    return StreamingResponse(
        result.body,
        media_type=result.media_type,
        headers=result.headers,
    )


@api_router.get("/audio/speech/live/{session_id}", tags=["audio"], summary="Stream live turn speech PCM")
def synthesize_live_speech_pcm(
    session_id: str,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> StreamingResponse:
    _ = authorization_header
    principal = request.state.principal
    session = _LIVE_TTS_SESSIONS.get(session_id)
    if session is None or session.user_id != principal.user_id:
        raise HTTPException(status_code=404, detail="tts session not found")
    return StreamingResponse(
        _stream_live_tts_pcm(session=session, request=request),
        media_type="audio/pcm",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-TTS-Format": "pcm_s16le",
            "X-TTS-Sample-Rate": str(session.config["sample_rate"]),
            "X-TTS-Channels": str(session.config["channels"]),
            "X-TTS-Sample-Width": str(session.config["sample_width"]),
        },
    )


@api_router.get("/audio/speech/models", tags=["audio"], summary="List TTS models")
def list_speech_models(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> dict[str, object]:
    _ = authorization_header
    principal = request.state.principal
    request_id = request.headers.get("x-request-id", "")
    return request.app.state.core_client.list_speech_models(
        user_id=principal.user_id,
        request_id=request_id,
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


@api_router.delete(
    "/chat/model-config/{model_config_id}",
    tags=["chat"],
    summary="Delete model config",
)
def delete_model_config(
    model_config_id: str,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.delete_model_config(
        user_id=principal.user_id,
        model_config_id=model_config_id,
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


# ── todos ──────────────────────────────────────────────────


@api_router.post("/todos", tags=["todos"], summary="Create todo")
def create_todo(
    req: TodoCreateRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.create_todo(
        user_id=principal.user_id,
        body=req.model_dump(mode="json"),
    )


@api_router.get("/todos", tags=["todos"], summary="List todos")
def list_todos(
    request: Request,
    status: str | None = None,
    include_deleted: bool = False,
    limit: int = 50,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.list_todos(
        user_id=principal.user_id,
        status=status,
        include_deleted=include_deleted,
        limit=limit,
    )


@api_router.get("/todos/{todo_id}", tags=["todos"], summary="Get todo")
def get_todo(
    todo_id: str,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.get_todo(
        user_id=principal.user_id,
        todo_id=todo_id,
    )


@api_router.patch("/todos/{todo_id}", tags=["todos"], summary="Update todo")
def update_todo(
    todo_id: str,
    req: TodoUpdateRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.update_todo(
        user_id=principal.user_id,
        todo_id=todo_id,
        body=req.model_dump(exclude_unset=True, mode="json"),
    )


@api_router.delete("/todos/{todo_id}", tags=["todos"], summary="Delete todo")
def delete_todo(
    todo_id: str,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    return request.app.state.core_client.delete_todo(
        user_id=principal.user_id,
        todo_id=todo_id,
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
    profile = enrich_runtime_profile_applications(req.model_dump())
    result = request.app.state.core_client.set_runtime_profile(
        user_id=principal.user_id,
        body=profile,
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


@api_router.post(
    "/client/vision/frame",
    tags=["execution"],
    summary="Push a real-time screen capture frame",
)
def push_vision_frame(
    body: VisionFramePushRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    _vision_frame_cache(request)[principal.user_id] = {
        "frame_base64": body.frame_base64,
        "mime_type": body.mime_type,
        "captured_at": body.captured_at,
        "sequence": body.sequence,
        "width": body.width,
        "height": body.height,
        "received_at": datetime.now().isoformat(),
    }
    return {"ok": True, "sequence": body.sequence}


@api_router.get(
    "/client/vision/frame",
    tags=["execution"],
    summary="Fetch the latest streamed screen capture frame",
)
def get_vision_frame(
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
):
    _ = authorization_header
    principal = request.state.principal
    frame = _vision_frame_cache(request).get(principal.user_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="no vision frame available")
    return frame


def _vision_frame_cache(request: Request) -> dict[str, dict[str, object]]:
    cache = getattr(request.app.state, "vision_frames", None)
    if isinstance(cache, dict):
        return cache
    request.app.state.vision_frames = {}
    return request.app.state.vision_frames


@api_router.post(
    "/deepthink/watch",
    tags=["execution"],
    summary="Run a bounded observe-act loop toward an open-ended goal",
)
def deepthink_watch(
    body: AutonomousLoopRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> StreamingResponse:
    _ = authorization_header
    principal = request.state.principal
    request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"
    turn_store = _turn_cancellation_store(request)
    previous_request_id = turn_store.begin_turn(
        user_id=principal.user_id,
        request_id=request_id,
        reason="autonomous_loop_started",
    )
    _cancel_pending_actions_for_turn(
        request=request,
        user_id=principal.user_id,
        request_id=previous_request_id,
        reason="autonomous_loop_started",
    )

    def is_cancelled() -> bool:
        return (
            turn_store.cancellation(user_id=principal.user_id, request_id=request_id)
            is not None
        )

    def stream() -> Generator[bytes, None, None]:
        try:
            yield from stream_autonomous_loop(
                core_client=request.app.state.core_client,
                action_dispatcher=request.app.state.action_dispatcher,
                request_id=request_id,
                user_id=principal.user_id,
                goal=body.goal,
                max_iterations=body.max_iterations,
                max_seconds=body.max_seconds,
                is_cancelled=is_cancelled,
            )
        finally:
            turn_store.finish_turn(user_id=principal.user_id, request_id=request_id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _background_loops(request: Request) -> dict[str, threading.Thread]:
    cache = getattr(request.app.state, "background_loops", None)
    if isinstance(cache, dict):
        return cache
    request.app.state.background_loops = {}
    return request.app.state.background_loops


def _start_background_autonomous_loop(
    *,
    request: Request,
    user_id: str,
    goal: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> str:
    """Runs the autonomous loop on a daemon thread, detached from whatever
    HTTP request triggered it — the caller doesn't need to keep a connection
    open. Actions still reach the client via the existing action_dispatcher
    queue / poller; a `notify` action (the loop's own "I'm done" signal)
    reaches the user the same way. Returns the request_id it's tracked under.

    Deliberately does NOT register with TurnCancellationStore: that store is
    one-active-turn-per-user (a new turn cancels the previous one, for
    barge-in) — reusing it here would cancel whatever conversation turn
    triggered this loop out from under itself. The loop gets its own
    independent cancel flag instead; it simply runs alongside the normal
    conversation turn rather than replacing it.
    """
    request_id = f"req_{uuid4().hex}"
    cancel_event = threading.Event()

    def is_cancelled() -> bool:
        return cancel_event.is_set()

    def run_and_cleanup() -> None:
        try:
            run_autonomous_loop(
                core_client=request.app.state.core_client,
                action_dispatcher=request.app.state.action_dispatcher,
                request_id=request_id,
                user_id=user_id,
                goal=goal,
                max_iterations=max_iterations,
                max_seconds=max_seconds,
                is_cancelled=is_cancelled,
            )
        except Exception:
            logger.exception(
                "autonomous loop background thread failed request_id=%s", request_id
            )
        finally:
            _background_loops(request).pop(request_id, None)

    thread = threading.Thread(
        target=run_and_cleanup, daemon=True, name=f"autonomous-loop-{request_id}"
    )
    _background_loops(request)[request_id] = thread
    thread.start()
    return request_id


@api_router.post(
    "/deepthink/watch/start",
    tags=["execution"],
    summary="Start a bounded observe-act loop in the background",
)
def deepthink_watch_start(
    body: AutonomousLoopRequest,
    request: Request,
    _: TokenAuth = None,
    authorization_header: AuthHeaderDoc = None,
) -> dict[str, object]:
    _ = authorization_header
    principal = request.state.principal
    request_id = _start_background_autonomous_loop(
        request=request,
        user_id=principal.user_id,
        goal=body.goal,
        max_iterations=body.max_iterations,
        max_seconds=body.max_seconds,
    )
    return {"request_id": request_id, "status": "started", "goal": body.goal}


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
