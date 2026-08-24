import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from jarvis_contracts import (
    ClientAction,
    ClientActionEnvelope,
    ClientActionResult,
    DeepThinkPlanResponse,
    DeepThinkResponse,
    DeepThinkStepPayload,
    DeepThinkStepResult,
    JarvisCoreEndpoints,
)
from jarvis_controller.app import SuppressPendingActionPollAccessLog, create_app
from jarvis_controller.middleware.core_client import (
    CoreBinaryResponse,
    CoreResponse,
    CoreStreamResponse,
)
from jarvis_controller.middleware.gateway_client import GatewayPrincipal

from router.router import _client_action_context


def _collect_events(body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    current_event: str | None = None
    data_lines: list[str] = []

    for line in body.splitlines():
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
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
        payload = json.loads("\n".join(data_lines) or "{}")
        events.append((current_event, payload))
    return events


class StubGatewayClient:
    def login(self, username: str, password: str, **kwargs) -> dict[str, object]:
        assert username == "admin"
        assert password == "admin123"
        return {
            "access_token": "token-123",
            "token_type": "bearer",
            "user_id": "u1",
        }

    def signup(
        self,
        email: str,
        name: str | None,
        password: str,
        **kwargs,
    ) -> dict[str, object]:
        assert email == "new-user@example.com"
        assert name == "New User"
        assert password == "secret"
        return {
            "access_token": "signup-token-456",
            "user_id": "u2",
            "email": email,
            "name": name,
            "role": "member",
        }

    def logout(self, token: str, **kwargs) -> dict[str, object]:
        assert token == "token-123"
        return {"ok": True}

    def validate_token(self, token: str, **kwargs) -> GatewayPrincipal:
        if token != "token-123":
            raise Exception("invalid or expired token")
        return GatewayPrincipal(user_id="u1", active=True)


class StubCoreClient:
    last_path: str | None = None
    last_chat_request: dict[str, object] | None = None
    last_chat_stream_request: dict[str, object] | None = None
    last_model_selection_request: dict[str, object] | None = None
    last_todo_request: dict[str, object] | None = None
    last_tts_request: dict[str, object] | None = None
    last_tts_pcm_request: dict[str, object] | None = None

    def chat_request(
        self,
        *,
        message: str,
        task_type: str = "general",
        confirm: bool = False,
        route_override: str | None = None,
        user_id: str,
        user_email: str = "",
        request_id: str = "",
    ) -> dict[str, object]:
        self.last_chat_request = {
            "message": message,
            "task_type": task_type,
            "confirm": confirm,
            "route_override": route_override,
            "user_id": user_id,
            "user_email": user_email,
            "request_id": request_id,
        }
        return {
            "request_id": request_id or "req-1",
            "route": route_override or "realtime",
            "provider_mode": "local",
            "provider_name": "stub-core",
            "model_name": "stub-model",
            "content": f"chat:{message}",
        }

    def run_realtime_conversation(self, message: str) -> CoreResponse:
        self.last_path = JarvisCoreEndpoints.INTERNAL_CONVERSATION_RESPOND.path
        return CoreResponse(
            mode="realtime",
            summary="stub realtime",
            content=f"실시간 응답: {message}",
            next_actions=["noop"],
        )

    def run_deep_thinking(self, message: str) -> CoreResponse:
        self.last_path = JarvisCoreEndpoints.INTERNAL_CONVERSATION_RESPOND.path
        return CoreResponse(
            mode="deep",
            summary="stub deep",
            content=f"Deep thinking result: {message}",
            next_actions=["inspect"],
        )

    def deepthink_plan(
        self,
        *,
        request_id: str,
        message: str,
        user_id: str,
    ) -> DeepThinkPlanResponse:
        return DeepThinkPlanResponse(
            request_id=request_id,
            goal=f"DeepThinking... {message}",
            steps=[
                DeepThinkStepPayload(
                    id="s1",
                    title="DeepThinking...",
                    description="Analyze the request",
                )
            ],
            constraints=[],
        )

    def deepthink_execute(
        self,
        *,
        request_id: str,
        message: str,
        plan_steps: list[dict[str, str]],
        user_id: str,
        execution_context: list[str] | None = None,
    ) -> DeepThinkResponse:
        step_id = plan_steps[0]["id"] if plan_steps else "s1"
        title = plan_steps[0]["title"] if plan_steps else "DeepThinking..."
        return DeepThinkResponse(
            request_id=request_id,
            steps=[
                DeepThinkStepResult(
                    step_id=step_id,
                    title=title,
                    status="completed",
                    content=f"DeepThinking... {message}",
                    actions=[],
                )
            ],
            summary="1/1 단계 완료",
            content=f"DeepThinking... {message}",
            actions=[],
        )

    def update_model_config(
        self,
        *,
        user_id: str,
        model_config_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        assert user_id == "u1"
        assert model_config_id == "mc1"
        return {"id": model_config_id, **body, "is_active": True}

    def delete_model_config(
        self,
        *,
        user_id: str,
        model_config_id: str,
    ) -> dict[str, object]:
        assert user_id == "u1"
        assert model_config_id == "mc1"
        return {"id": model_config_id, "deleted": True}

    def set_model_selection(
        self,
        *,
        user_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        assert user_id == "u1"
        self.last_model_selection_request = {"user_id": user_id, "body": body}
        return {
            "realtime_model_config_id": body.get("realtime_model_config_id"),
            "deep_model_config_id": body.get("deep_model_config_id"),
        }

    def get_model_selection(self, *, user_id: str) -> dict[str, object]:
        assert user_id == "u1"
        return {
            "realtime_model_config_id": "rt-model-config",
            "deep_model_config_id": "deep-model-config",
        }

    def synthesize_speech(
        self,
        *,
        user_id: str,
        body: dict[str, object],
        request_id: str = "",
    ) -> CoreBinaryResponse:
        self.last_tts_request = {
            "user_id": user_id,
            "body": body,
            "request_id": request_id,
        }
        return CoreBinaryResponse(
            content=b"audio-bytes",
            media_type="audio/mpeg",
            headers={
                "x-tts-provider": "openai",
                "x-tts-model": "gpt-4o-mini-tts",
                "x-tts-voice": "marin",
                "x-ai-generated-voice": "true",
            },
        )

    def synthesize_speech_pcm_stream(
        self,
        *,
        user_id: str,
        body: dict[str, object],
        request_id: str = "",
    ) -> CoreStreamResponse:
        self.last_tts_pcm_request = {
            "user_id": user_id,
            "body": body,
            "request_id": request_id,
        }
        chunks = body.get("chunks")
        chunk_count = len(chunks) if isinstance(chunks, list) else 0
        return CoreStreamResponse(
            body=iter([b"pcm-1", b"pcm-2"]),
            media_type="audio/pcm",
            headers={
                "x-tts-provider": "server",
                "x-tts-format": "pcm_s16le",
                "x-tts-sample-rate": "24000",
                "x-tts-channels": "1",
                "x-tts-sample-width": "2",
                "x-tts-chunk-count": str(chunk_count),
                "x-ai-generated-voice": "true",
            },
        )

    def list_speech_models(
        self,
        *,
        user_id: str,
        request_id: str = "",
    ) -> dict[str, object]:
        return {
            "models": [
                {
                    "id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    "label": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    "provider": "qwen",
                    "is_default": True,
                },
                {
                    "id": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                    "label": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                    "provider": "qwen",
                    "is_default": False,
                },
            ]
        }

    def create_todo(self, *, user_id: str, body: dict[str, object]) -> dict[str, object]:
        self.last_todo_request = {"method": "create", "user_id": user_id, "body": body}
        return {
            "id": "todo-1",
            "user_id": user_id,
            **body,
            "status": "open",
            "priority": body.get("priority", 3),
            "calendar_sync_status": "none",
            "metadata": body.get("metadata", {}),
            "created_at": "2026-05-13T00:00:00+00:00",
            "updated_at": "2026-05-13T00:00:00+00:00",
            "completed_at": None,
        }

    def list_todos(
        self,
        *,
        user_id: str,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> dict[str, object]:
        self.last_todo_request = {
            "method": "list",
            "user_id": user_id,
            "status": status,
            "include_deleted": include_deleted,
            "limit": limit,
        }
        return {
            "items": [
                {
                    "id": "todo-1",
                    "user_id": user_id,
                    "title": "테스트 todo",
                    "status": status or "open",
                }
            ]
        }

    def get_todo(self, *, user_id: str, todo_id: str) -> dict[str, object]:
        self.last_todo_request = {"method": "get", "user_id": user_id, "todo_id": todo_id}
        return {"id": todo_id, "user_id": user_id, "title": "todo"}

    def update_todo(
        self,
        *,
        user_id: str,
        todo_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        self.last_todo_request = {
            "method": "update",
            "user_id": user_id,
            "todo_id": todo_id,
            "body": body,
        }
        return {"id": todo_id, "user_id": user_id, **body}

    def delete_todo(self, *, user_id: str, todo_id: str) -> dict[str, object]:
        self.last_todo_request = {
            "method": "delete",
            "user_id": user_id,
            "todo_id": todo_id,
        }
        return {"id": todo_id, "deleted": True}

    def chat_stream(
        self,
        *,
        message: str,
        task_type: str = "general",
        confirm: bool = False,
        route_override: str | None = None,
        user_id: str,
        user_email: str = "",
        request_id: str = "",
    ):
        self.last_chat_stream_request = {
            "message": message,
            "task_type": task_type,
            "confirm": confirm,
            "route_override": route_override,
            "user_id": user_id,
            "user_email": user_email,
            "request_id": request_id,
        }
        yield b'event: assistant_delta\ndata: {"content":"stub "}\n\n'
        yield b'event: assistant_done\ndata: {"content":"stub response"}\n\n'

stub_core_client = StubCoreClient()
client = TestClient(
    create_app(gateway_client=StubGatewayClient(), core_client=stub_core_client)
)


def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer token-123", "x-client-id": "test-client"}


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["action_runtime"]["core_fallback_enabled"] is True


def test_swagger_docs_are_public() -> None:
    response = client.get("/docs")

    assert response.status_code == 200
    assert "Swagger UI" in response.text


def test_openapi_includes_bearer_security_scheme() -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    security_scheme = schema["components"]["securitySchemes"]["HTTPBearer"]
    assert security_scheme["type"] == "http"
    assert security_scheme["scheme"] == "bearer"
    assert schema["paths"]["/auth/me"]["get"]["security"] == [{"HTTPBearer": []}]
    parameters = schema["paths"]["/auth/me"]["get"]["parameters"]
    assert any(
        parameter["name"] == "Authorization" and parameter["in"] == "header"
        for parameter in parameters
    )


def test_login_proxies_to_gateway() -> None:
    response = client.post(
        "/auth/login",
        json={"username": "admin", "password": "admin123"},
        headers={"x-client-id": "test-client"},
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == "token-123"


def test_signup_proxies_to_gateway_signup() -> None:
    response = client.post(
        "/auth/signup",
        json={
            "email": "new-user@example.com",
            "name": "New User",
            "password": "secret",
        },
        headers={"x-client-id": "test-client"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "u2"
    assert payload["email"] == "new-user@example.com"
    assert payload["access_token"] == "signup-token-456"
    assert "role" not in payload
    assert "tenant_id" not in payload


def test_auth_me_uses_gateway_validation() -> None:
    response = client.get("/auth/me", headers=auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "u1"
    assert payload["active"] is True


def test_update_model_config_proxies_to_core() -> None:
    response = client.put(
        "/chat/model-config/mc1",
        json={
            "provider_mode": "local",
            "provider_name": "docker-model-runner",
            "model_name": "docker.io/ai/gemma3-qat:4B",
            "api_key": "",
            "endpoint": "https://qwen.breakpack.cc/engines/v1/chat/completions",
            "is_default": False,
            "supports_stream": True,
            "supports_realtime": True,
            "transport": "http_sse",
            "input_modalities": "text",
            "output_modalities": "text",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "mc1"
    assert payload["provider_name"] == "docker-model-runner"
    assert payload["model_name"] == "docker.io/ai/gemma3-qat:4B"


def test_delete_model_config_proxies_to_core() -> None:
    response = client.delete("/chat/model-config/mc1", headers=auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"id": "mc1", "deleted": True}


def test_set_model_selection_proxies_to_core() -> None:
    stub_core_client.last_model_selection_request = None

    response = client.post(
        "/chat/model-selection",
        json={
            "realtime_model_config_id": "rt-model-config",
            "deep_model_config_id": "deep-model-config",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "realtime_model_config_id": "rt-model-config",
        "deep_model_config_id": "deep-model-config",
    }
    assert stub_core_client.last_model_selection_request == {
        "user_id": "u1",
        "body": {
            "realtime_model_config_id": "rt-model-config",
            "deep_model_config_id": "deep-model-config",
        },
    }


def test_get_model_selection_proxies_to_core() -> None:
    response = client.get("/chat/model-selection", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {
        "realtime_model_config_id": "rt-model-config",
        "deep_model_config_id": "deep-model-config",
    }


def test_audio_speech_proxies_to_core_with_current_user() -> None:
    stub_core_client.last_tts_request = None

    response = client.post(
        "/audio/speech",
        json={
            "text": "안녕하세요",
            "voice": "marin",
            "response_format": "mp3",
        },
        headers={**auth_headers(), "x-request-id": "r-tts"},
    )

    assert response.status_code == 200
    assert response.content == b"audio-bytes"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-tts-provider"] == "openai"
    assert response.headers["x-ai-generated-voice"] == "true"
    assert stub_core_client.last_tts_request == {
        "user_id": "u1",
        "body": {
            "text": "안녕하세요",
            "provider": "openai",
            "model": "gpt-4o-mini-tts",
            "voice": "marin",
            "response_format": "mp3",
        },
        "request_id": "r-tts",
    }


def test_audio_speech_pcm_streams_core_response_with_current_user() -> None:
    stub_core_client.last_tts_pcm_request = None

    response = client.post(
        "/audio/speech/pcm",
        json={
            "chunks": [
                {
                    "id": "c-1770000000000",
                    "text": "안녕하세요. 오늘 일정 요약해드릴게요.",
                },
            ],
            "voice": "marin",
            "model": "gpt-4o-mini-tts",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "format": "pcm_s16le",
        },
        headers={**auth_headers(), "x-request-id": "r-tts-pcm"},
    )

    assert response.status_code == 200
    assert response.content == b"pcm-1pcm-2"
    assert response.headers["content-type"] == "audio/pcm"
    assert response.headers["x-tts-provider"] == "server"
    assert response.headers["x-tts-format"] == "pcm_s16le"
    assert response.headers["x-tts-chunk-count"] == "1"
    assert stub_core_client.last_tts_pcm_request == {
        "user_id": "u1",
        "body": {
            "chunks": [
                {
                    "id": "c-1770000000000",
                    "text": "안녕하세요. 오늘 일정 요약해드릴게요.",
                },
            ],
            "voice": "default",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "format": "pcm_s16le",
        },
        "request_id": "r-tts-pcm",
    }


def test_audio_speech_pcm_preserves_user_selected_model() -> None:
    stub_core_client.last_tts_pcm_request = None

    response = client.post(
        "/audio/speech/pcm",
        json={
            "chunks": [{"id": "c-1770000000000", "text": "안녕하세요."}],
            "voice": "default",
            "model": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "format": "pcm_s16le",
        },
        headers={**auth_headers(), "x-request-id": "r-tts-selected-model"},
    )

    assert response.status_code == 200
    assert stub_core_client.last_tts_pcm_request
    assert stub_core_client.last_tts_pcm_request["body"]["model"] == (
        "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    )


def test_audio_speech_models_proxies_to_core_with_current_user() -> None:
    response = client.get(
        "/audio/speech/models",
        headers={**auth_headers(), "x-request-id": "r-tts-models"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["models"][0]["id"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"


def test_todo_routes_proxy_to_core_with_current_user() -> None:
    create_response = client.post(
        "/todos",
        json={
            "title": "소불고기 재료 사기",
            "due_at": "2026-05-20T09:00:00+09:00",
            "metadata": {"source": "chat"},
        },
        headers=auth_headers(),
    )
    assert create_response.status_code == 200
    assert create_response.json()["id"] == "todo-1"
    assert stub_core_client.last_todo_request == {
        "method": "create",
        "user_id": "u1",
        "body": {
            "title": "소불고기 재료 사기",
            "description": None,
            "priority": 3,
            "due_at": "2026-05-20T09:00:00+09:00",
            "remind_at": None,
            "timezone": None,
            "calendar_provider": None,
            "calendar_id": None,
            "calendar_event_id": None,
            "chat_id": None,
            "source_message_id": None,
            "metadata": {"source": "chat"},
        },
    }

    list_response = client.get(
        "/todos?status=open&include_deleted=true&limit=10",
        headers=auth_headers(),
    )
    assert list_response.status_code == 200
    assert stub_core_client.last_todo_request == {
        "method": "list",
        "user_id": "u1",
        "status": "open",
        "include_deleted": True,
        "limit": 10,
    }

    get_response = client.get("/todos/todo-1", headers=auth_headers())
    assert get_response.status_code == 200
    assert stub_core_client.last_todo_request == {
        "method": "get",
        "user_id": "u1",
        "todo_id": "todo-1",
    }

    update_response = client.patch(
        "/todos/todo-1",
        json={"status": "completed"},
        headers=auth_headers(),
    )
    assert update_response.status_code == 200
    assert stub_core_client.last_todo_request == {
        "method": "update",
        "user_id": "u1",
        "todo_id": "todo-1",
        "body": {"status": "completed"},
    }

    delete_response = client.delete("/todos/todo-1", headers=auth_headers())
    assert delete_response.status_code == 200
    assert stub_core_client.last_todo_request == {
        "method": "delete",
        "user_id": "u1",
        "todo_id": "todo-1",
    }


def test_chat_request_can_escalate_to_deep() -> None:
    response = client.post(
        "/chat/request",
        json={
            "message": "원인 깊게 분석해줘",
            "thinking_mode": "deep",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "deep"
    assert stub_core_client.last_chat_request is not None
    assert stub_core_client.last_chat_request["route_override"] == "deep"


def test_conversation_endpoint_routes_realtime_to_core() -> None:
    response = client.post(
        "/conversation/respond",
        json={
            "message": "배포 상태 알려줘",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "realtime"
    assert payload["handler"] == "jarvis-core"
    assert "실시간 응답" in payload["content"]
    assert stub_core_client.last_path == JarvisCoreEndpoints.INTERNAL_CONVERSATION_RESPOND.path


def test_conversation_endpoint_keeps_planning_in_controller() -> None:
    response = client.post(
        "/conversation/respond",
        json={
            "message": "작업 계획 세워줘\n1. 요구사항 정리\n2. 검증 정의",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "planning"
    assert payload["handler"] == "jarvis-controller"
    assert payload["planning"]["steps"][0]["description"] == "요구사항 정리"


def test_conversation_stream_emits_classification_for_general_query() -> None:
    response = client.post(
        "/conversation/stream",
        json={"message": "배포 상태 알려줘"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert "event: classification" in body
    assert '"category": "general"' in body
    assert "event: assistant_delta" in body
    assert stub_core_client.last_chat_stream_request is not None
    assert stub_core_client.last_chat_stream_request["route_override"] == "realtime"


def test_realtime_stream_does_not_wait_for_slow_action_classifier(monkeypatch) -> None:
    def slow_no_action(*args, **kwargs):
        time.sleep(0.4)
        return None

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        slow_no_action,
    )
    monkeypatch.setenv("JARVIS_ACTION_ARBITRATION_BUFFER_SECONDS", "0.01")
    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS", "0.01")

    started = time.monotonic()
    response = client.post(
        "/conversation/stream",
        json={"message": "빠르게 일반 대화 응답해줘"},
        headers=auth_headers(),
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.25
    assert "event: assistant_delta" in response.text


def test_conversation_stream_first_event_is_immediate(monkeypatch) -> None:
    def slow_no_action(*args, **kwargs):
        time.sleep(0.4)
        return None

    def slow_realtime_decision(*args, **kwargs):
        from planner.conversation_routing import ConversationMode, RoutingDecision

        time.sleep(0.4)
        return RoutingDecision(
            mode=ConversationMode.REALTIME,
            triggered=False,
            confidence=0.8,
            reasons=["slow test routing"],
        )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        slow_no_action,
    )
    monkeypatch.setattr("router.router.evaluate_conversation_mode", slow_realtime_decision)
    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS", "0.01")

    started = time.monotonic()
    with client.stream(
        "POST",
        "/conversation/stream",
        json={"message": "빠르게 일반 대화 응답해줘"},
        headers=auth_headers(),
    ) as response:
        first_text = next(response.iter_text())
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.25
    assert "event: assistant_delta" in first_text


def test_chat_stream_auto_first_event_is_immediate(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS", "0.01")

    def slow_no_action(*args, **kwargs):
        time.sleep(0.4)
        return None

    def slow_realtime_decision(*args, **kwargs):
        from planner.conversation_routing import ConversationMode, RoutingDecision

        time.sleep(0.4)
        return RoutingDecision(
            mode=ConversationMode.REALTIME,
            triggered=False,
            confidence=0.8,
            reasons=["slow test routing"],
        )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        slow_no_action,
    )
    monkeypatch.setattr("router.router.evaluate_conversation_mode", slow_realtime_decision)

    started = time.monotonic()
    with client.stream(
        "POST",
        "/chat/stream",
        json={"message": "빠르게 일반 대화 응답해줘", "thinking_mode": "auto"},
        headers=auth_headers(),
    ) as response:
        first_text = next(response.iter_text())
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.25
    assert "event: assistant_delta" in first_text


def test_fast_direct_action_runs_after_realtime_starts(monkeypatch) -> None:
    from planner.action_intent_classifier import ActionIntentDecision

    action = ClientAction(
        type="open_url",
        command=None,
        target="https://example.com",
        args={},
        description="open example",
        requires_confirm=False,
    )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="open_url",
            confidence=0.9,
            reason="model action",
            actions=[action],
        ),
    )

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_parallel",
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

    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "example.com 열어줘"},
            headers=auth_headers(),
        )
    finally:
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "event: action_dispatch" in body
    assert "event: assistant_delta" in body
    assert "진행하겠습니다!" not in body
    assert "요청한 작업을 실행했습니다." in body
    assert body.index("event: action_result") < body.index("요청한 작업을 실행했습니다.")


def test_action_candidate_waits_past_short_done_grace(monkeypatch) -> None:
    from planner.action_intent_classifier import ActionIntentDecision

    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS", "0.01")
    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_CAP_SECONDS", "0.01")
    monkeypatch.setenv("JARVIS_ACTION_CANDIDATE_WAIT_SECONDS", "0.5")
    monkeypatch.setenv("JARVIS_ACTION_CANDIDATE_WAIT_CAP_SECONDS", "0.5")

    action = ClientAction(
        type="app_control",
        command="open",
        target="Sublime Text",
        args={},
        description="Open app",
        requires_confirm=False,
    )

    def slow_action(*args, **kwargs):
        time.sleep(0.12)
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="app.open",
            confidence=0.9,
            reason="model action",
            actions=[action],
        )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        slow_action,
    )

    original_chat_stream = stub_core_client.chat_stream

    def fast_done_stream(**kwargs):
        yield b'event: assistant_delta\ndata: {"content":"stub "}\n\n'
        yield b'event: assistant_done\ndata: {"content":"stub response"}\n\n'

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_candidate_wait",
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

    stub_core_client.chat_stream = fast_done_stream
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "앱에서 sublimetext 켜줘"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "event: action_dispatch" in body
    assert "event: assistant_done" in body
    assert body.index("event: action_dispatch") < body.index("event: assistant_done")


def test_local_browser_search_template_removes_action_framing(monkeypatch) -> None:
    def fail_if_model_called(*args, **kwargs):
        raise AssertionError("local browser search template should bypass model")

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        fail_if_model_called,
    )

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_local_search",
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

    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저 열어서 소불고기 레시피 검색해줘"},
            headers=auth_headers(),
        )
    finally:
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    events = _collect_events(response.text)
    dispatches = [payload for event, payload in events if event == "action_dispatch"]
    assert dispatches
    action = dispatches[0]["action"]
    assert action["type"] == "open_url"
    assert action["args"]["query"] == "소불고기 레시피"
    assert "브라우저" not in action["target"]


def test_direct_action_ready_at_done_emits_before_assistant_done(monkeypatch) -> None:
    from planner.action_intent_classifier import ActionIntentDecision

    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS", "0.5")

    action = ClientAction(
        type="open_url",
        command=None,
        target="about:blank",
        args={"browser": "default"},
        description="open browser",
        requires_confirm=False,
    )

    def slow_action(*args, **kwargs):
        time.sleep(0.15)
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser.open",
            confidence=0.9,
            reason="model action",
            actions=[action],
        )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        slow_action,
    )

    original_chat_stream = stub_core_client.chat_stream

    def fast_done_stream(**kwargs):
        yield b'event: assistant_delta\ndata: {"content":"stub "}\n\n'
        yield b'event: assistant_done\ndata: {"content":"stub response"}\n\n'

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_done_grace",
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

    stub_core_client.chat_stream = fast_done_stream
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저 열어줘"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "event: assistant_delta" in body
    assert "event: action_dispatch" in body
    assert "event: assistant_done" in body
    assert "진행하겠습니다!" not in body
    assert "요청한 작업을 실행했습니다." in body
    assert body.index("event: action_dispatch") < body.index("event: assistant_done")


def test_action_ack_done_waits_for_recovery_dispatch(monkeypatch) -> None:
    from planner.action_intent_classifier import ActionIntentDecision

    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS", "0.01")
    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_CAP_SECONDS", "0.01")
    monkeypatch.setenv("JARVIS_ACTION_ACK_RECOVERY_GRACE_SECONDS", "0.5")
    monkeypatch.setenv("JARVIS_ACTION_ACK_RECOVERY_GRACE_CAP_SECONDS", "0.5")

    action = ClientAction(
        type="browser",
        command="open",
        target=None,
        args={"browser": "chrome"},
        description="Open browser",
        requires_confirm=False,
    )

    def slow_action(*args, **kwargs):
        time.sleep(0.12)
        return ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser",
            confidence=0.95,
            reason="model action",
            actions=[action],
        )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        slow_action,
    )

    original_chat_stream = stub_core_client.chat_stream

    def ack_only_stream(**kwargs):
        yield 'event: assistant_delta\ndata: {"content":"진행하겠습니다!"}\n\n'.encode()
        yield 'event: assistant_done\ndata: {"content":"진행하겠습니다!"}\n\n'.encode()

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_ack_recovered",
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

    stub_core_client.chat_stream = ack_only_stream
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저 열어줘"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "event: action_dispatch" in body
    assert "action_decision_timeout" not in body
    assert "진행하겠습니다!" not in body
    assert "요청한 작업을 실행했습니다." in body
    assert body.index("event: action_result") < body.index("요청한 작업을 실행했습니다.")


def test_stream_realtime_emits_text_plan_step_progress(monkeypatch) -> None:
    original_chat_stream = stub_core_client.chat_stream
    try:
        monkeypatch.setattr(
            "router.router.classify_client_action_intent_decision",
            lambda *args, **kwargs: None,
        )
        def fake_chat_stream(**kwargs):
            yield b'event: assistant_delta\ndata: {"content":"hello "}\n\n'
            yield b'event: assistant_done\ndata: {"content":"hello world"}\n\n'

        stub_core_client.chat_stream = fake_chat_stream
        response = client.post(
            "/conversation/stream",
            json={"message": "안녕"},
            headers=auth_headers(),
        )

        assert response.status_code == 200
        events = _collect_events(response.text)
        event_types = [event for event, _ in events]
        assert "assistant_delta" in event_types
        assert "assistant_done" in event_types
        assert "plan_step" in event_types

        plan_steps = [payload for event, payload in events if event == "plan_step"]
        assert any(step.get("status") == "in_progress" for step in plan_steps)
        assert any(step.get("status") == "completed" for step in plan_steps)
    finally:
        stub_core_client.chat_stream = original_chat_stream


def test_stream_direct_action_emits_plan_steps(monkeypatch) -> None:
    from planner.action_intent_classifier import ActionIntentDecision

    action = ClientAction(
        type="open_url",
        command=None,
        target="https://example.com",
        args={"browser": "default"},
        description="open example page",
        requires_confirm=False,
    )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="open_url",
            confidence=0.9,
            reason="test action",
            actions=[action],
        ),
    )

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_direct_steps",
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

    original_dispatcher = client.app.state.action_dispatcher
    original_chat_stream = stub_core_client.chat_stream
    try:
        stub_core_client.chat_stream = lambda **kwargs: iter(())
        client.app.state.action_dispatcher = CompletedDispatcher()

        response = client.post(
            "/conversation/stream",
            json={"message": "sublimetext 켜서 안녕"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    events = _collect_events(response.text)
    plan_steps = [payload for event, payload in events if event == "plan_step"]
    assert plan_steps
    step_ids = [step.get("id") for step in plan_steps]
    assert "act_direct_steps" in step_ids
    direct_steps = [step for step in plan_steps if step.get("id") == "act_direct_steps"]
    assert any(step.get("status") == "queued" for step in direct_steps)
    assert any(step.get("status") == "in_progress" for step in direct_steps)
    assert any(step.get("status") == "completed" for step in direct_steps)
    direct_statuses = [step.get("status") for step in direct_steps]
    assert direct_statuses.index("queued") < direct_statuses.index("in_progress")
    assert direct_statuses.index("in_progress") < direct_statuses.index("completed")


def test_conversation_stream_starts_realtime_before_slow_deep_routing(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS", "0.01")

    def slow_deep_decision(*args, **kwargs):
        from planner.conversation_routing import ConversationMode, RoutingDecision

        time.sleep(0.4)
        return RoutingDecision(
            mode=ConversationMode.DEEP,
            triggered=True,
            confidence=0.95,
            reasons=["slow deep route"],
        )

    monkeypatch.setattr("router.router.evaluate_conversation_mode", slow_deep_decision)

    started = time.monotonic()
    with client.stream(
        "POST",
        "/conversation/stream",
        json={"message": "이 에러 로그 원인 깊게 분석해줘\nTraceback: boom"},
        headers=auth_headers(),
    ) as response:
        first_text = next(response.iter_text())
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.25
    assert "event: assistant_delta" in first_text


def test_execute_mock_success() -> None:
    response = client.post(
        "/execute",
        json={
            "request_id": "r1",
            "action": "click",
            "target": "#submit",
            "contract_version": "1.0",
        },
        headers=auth_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["output"]["mock"] is True


def test_client_action_pending_and_result_endpoints() -> None:
    envelope = client.app.state.action_dispatcher.enqueue(
        user_id="u1",
        request_id="req-client-action",
        action=ClientAction(
            type="browser_control",
            command="scroll",
            target="active_tab",
            args={"direction": "down", "amount": "page"},
            description="현재 브라우저 페이지를 아래로 스크롤",
            requires_confirm=False,
        ),
    )

    pending_response = client.get(
        "/client/actions/pending",
        headers=auth_headers(),
    )

    assert pending_response.status_code == 200
    pending = pending_response.json()
    assert pending[0]["action_id"] == envelope.action_id
    assert pending[0]["action"]["type"] == "browser_control"

    result_response = client.post(
        f"/client/actions/{envelope.action_id}/result",
        json={
            "status": "completed",
            "output": {"scroll_y": 1200},
            "contract_version": "1.0",
        },
        headers=auth_headers(),
    )

    assert result_response.status_code == 200
    result = result_response.json()
    assert result["status"] == "completed"
    assert result["output"]["scroll_y"] == 1200


def test_vision_frame_push_and_fetch_roundtrip() -> None:
    missing = client.get("/client/vision/frame", headers=auth_headers())
    assert missing.status_code == 404

    pushed = client.post(
        "/client/vision/frame",
        json={
            "frame_base64": "ZmFrZS1qcGVn",
            "mime_type": "image/jpeg",
            "sequence": 1,
            "width": 1280,
            "height": 720,
        },
        headers=auth_headers(),
    )
    assert pushed.status_code == 200
    assert pushed.json()["sequence"] == 1

    fetched = client.get("/client/vision/frame", headers=auth_headers())
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["frame_base64"] == "ZmFrZS1qcGVn"
    assert body["width"] == 1280

    pushed_again = client.post(
        "/client/vision/frame",
        json={"frame_base64": "c2Vjb25kLWZyYW1l", "sequence": 2},
        headers=auth_headers(),
    )
    assert pushed_again.status_code == 200
    latest = client.get("/client/vision/frame", headers=auth_headers())
    assert latest.json()["frame_base64"] == "c2Vjb25kLWZyYW1l"


def test_client_action_result_updates_backend_action_state() -> None:
    envelope = client.app.state.action_dispatcher.enqueue(
        user_id="u1",
        request_id="req-client-action-state",
        action=ClientAction(
            type="open_url",
            command=None,
            target="https://www.google.com/search?q=openai",
            args={"query": "openai", "browser": "chrome"},
            description="Search openai",
            requires_confirm=False,
        ),
    )

    response = client.post(
        f"/client/actions/{envelope.action_id}/result",
        json={
            "status": "completed",
            "output": {"opened": "https://www.google.com/search?q=openai"},
            "contract_version": "1.0",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    browser_context = client.app.state.action_context.browser_context("u1")
    assert browser_context is not None
    assert browser_context.last_query == "openai"
    assert browser_context.last_url == "https://www.google.com/search?q=openai"
    latest_result = client.app.state.action_context.latest_result("u1")
    assert latest_result is not None
    assert latest_result.action_type == "open_url"
    assert latest_result.output["opened"] == "https://www.google.com/search?q=openai"


def test_action_dispatcher_cancel_request_rejects_pending_action() -> None:
    envelope = client.app.state.action_dispatcher.enqueue(
        user_id="u1",
        request_id="req-client-action-cancel",
        action=ClientAction(
            type="browser",
            command="open",
            target=None,
            args={"browser": "chrome"},
            description="Open browser",
            requires_confirm=False,
        ),
    )

    cancelled = client.app.state.action_dispatcher.cancel_request(
        user_id="u1",
        request_id="req-client-action-cancel",
        reason="barge_in",
    )

    assert cancelled == 1
    result = client.app.state.action_dispatcher.wait_for_result(
        action_id=envelope.action_id,
        request_id="req-client-action-cancel",
        timeout_seconds=0.01,
    )
    assert result.status == "rejected"
    assert result.error == "cancelled: barge_in"
    assert result.output["cancelled"] is True


def test_conversation_cancel_endpoint_cancels_pending_action() -> None:
    envelope = client.app.state.action_dispatcher.enqueue(
        user_id="u1",
        request_id="req-conversation-cancel",
        action=ClientAction(
            type="open_url",
            command=None,
            target="https://www.google.com/search?q=test",
            args={"browser": "chrome", "query": "test"},
            description="Search browser",
            requires_confirm=False,
        ),
    )

    response = client.post(
        "/conversation/cancel",
        json={"request_id": "req-conversation-cancel", "reason": "barge_in"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cancelled"] is True
    assert payload["request_id"] == "req-conversation-cancel"
    assert payload["cancelled_actions"] == 1
    result = client.app.state.action_dispatcher.wait_for_result(
        action_id=envelope.action_id,
        request_id="req-conversation-cancel",
        timeout_seconds=0.01,
    )
    assert result.status == "rejected"
    assert result.output["reason"] == "barge_in"


def test_conversation_stream_cancels_previous_turn_actions() -> None:
    store = client.app.state.turn_cancellation
    store.begin_turn(user_id="u1", request_id="req-old-turn", reason="barge_in")
    envelope = client.app.state.action_dispatcher.enqueue(
        user_id="u1",
        request_id="req-old-turn",
        action=ClientAction(
            type="browser",
            command="open",
            target=None,
            args={"browser": "chrome"},
            description="Open browser",
            requires_confirm=False,
        ),
    )

    response = client.post(
        "/conversation/stream",
        json={"message": "안녕?"},
        headers={**auth_headers(), "x-request-id": "req-new-turn"},
    )

    assert response.status_code == 200
    result = client.app.state.action_dispatcher.wait_for_result(
        action_id=envelope.action_id,
        request_id="req-old-turn",
        timeout_seconds=0.01,
    )
    assert result.status == "rejected"
    assert store.cancellation(user_id="u1", request_id="req-old-turn") is not None


def test_client_screenshot_result_updates_latest_observation_state() -> None:
    envelope = client.app.state.action_dispatcher.enqueue(
        user_id="u1",
        request_id="req-client-observation",
        action=ClientAction(
            type="screenshot",
            command=None,
            target="full_screen",
            args={},
            description="Capture screen",
            requires_confirm=False,
        ),
    )

    response = client.post(
        f"/client/actions/{envelope.action_id}/result",
        json={
            "status": "completed",
            "output": {"image_path": "/tmp/screen.png", "summary": "desktop"},
            "contract_version": "1.0",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    latest_observation = client.app.state.action_context.latest_observation("u1")
    assert latest_observation is not None
    assert latest_observation.action_type == "screenshot"
    assert latest_observation.output["summary"] == "desktop"


def test_client_action_context_includes_working_context() -> None:
    client.app.state.action_context.record_action_result(
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
        action_id="act_context_app",
    )
    client.app.state.action_context.record_action_result(
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
        output={},
        action_id="act_context_type",
    )

    request = type(
        "Request",
        (),
        {
            "app": client.app,
            "headers": {},
        },
    )()

    context = _client_action_context(request=request, user_id="u1")

    assert context is not None
    working_context = context["working_context"]
    assert isinstance(working_context, dict)
    assert working_context["active_app"] == "Sublime Text"
    assert working_context["last_typed_text"] == "안녕하세요. 저는 JARVIS입니다."
    assert working_context["last_typed_target"] == "Sublime Text"


def test_stream_suppresses_invalid_embedded_browser_app_action(monkeypatch) -> None:
    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: None,
    )
    original_chat_stream = stub_core_client.chat_stream

    def fake_chat_stream(**kwargs):
        yield (
            b"event: assistant_done\n"
            b'data: {"content":"```actions\\n'
            b'{\\"type\\":\\"app_control\\",\\"command\\":\\"open\\",'
            b'\\"target\\":\\"browser\\",\\"args\\":{},'
            b'\\"description\\":\\"x\\",\\"requires_confirm\\":false}'
            b'\\n```"}\n\n'
        )

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_recovered",
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

    stub_core_client.chat_stream = fake_chat_stream
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저 켜서 연어장 찾아줘"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "embedded assistant action suppressed" in body
    assert "실행할 액션을 큐에 넣지 못해 실행하지 않았습니다." in body
    assert "action_dispatch" not in body


def test_stream_suppresses_valid_embedded_action_block_without_backend_queue(monkeypatch) -> None:
    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: None,
    )
    original_chat_stream = stub_core_client.chat_stream

    def fake_chat_stream(**kwargs):
        yield (
            b"event: assistant_done\n"
            b'data: {"content":"```actions\\n'
            b'{\\"name\\":\\"browser.search\\",'
            b'\\"args\\":{\\"query\\":\\"salmon\\"},'
            b'\\"description\\":\\"search salmon\\",'
            b'\\"requires_confirm\\":false}'
            b'\\n```"}\n\n'
        )

    class FailIfQueuedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            raise AssertionError("embedded assistant text must not be queued")

        def wait_for_result(self, *, action_id, request_id, timeout_seconds=None):
            raise AssertionError("embedded assistant text must not run")

    stub_core_client.chat_stream = fake_chat_stream
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = FailIfQueuedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저에서 연어장 검색해줘"},
            headers={
                **auth_headers(),
                "x-client-enabled-capabilities": "browser.search,browser.navigate,browser.open",
            },
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "embedded assistant action suppressed" in body
    assert "compiler_unavailable" in body
    assert "action_dispatch" not in body


def test_stream_suppresses_bash_action_block_without_backend_queue(monkeypatch) -> None:
    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: None,
    )
    original_chat_stream = stub_core_client.chat_stream

    def fake_chat_stream(**kwargs):
        yield (
            b"event: assistant_done\n"
            b'data: {"content":"open it\\n```bash\\nopen https://example.com\\n```"}\n\n'
        )

    class FailIfQueuedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            raise AssertionError("assistant bash text must not be queued")

        def wait_for_result(self, *, action_id, request_id, timeout_seconds=None):
            raise AssertionError("assistant bash text must not run")

    stub_core_client.chat_stream = fake_chat_stream
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = FailIfQueuedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저에서 example 검색해줘"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "embedded assistant action suppressed" in body
    assert "action_dispatch" not in body
    assert "open https://example.com" not in body


def test_stream_recovers_invalid_embedded_action_with_action_classifier(monkeypatch) -> None:
    from planner.action_intent_classifier import ActionIntentDecision

    recovered_action = ClientAction(
        type="open_url",
        command=None,
        target="https://www.google.com/search?q=%EC%97%B0%EC%96%B4%EC%9E%A5",
        args={"browser": "chrome", "query": "연어장"},
        description="브라우저에서 연어장 검색",
        requires_confirm=False,
    )

    def fake_action_compiler(*args, **kwargs):
        if kwargs.get("validation_errors"):
            return ActionIntentDecision(
                should_act=True,
                execution_mode="direct",
                intent="open_url",
                confidence=0.91,
                reason="model retry",
                actions=[recovered_action],
            )
        return None

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        fake_action_compiler,
    )
    original_chat_stream = stub_core_client.chat_stream

    def fake_chat_stream(**kwargs):
        yield (
            b"event: assistant_done\n"
            b'data: {"content":"```actions\\n'
            b'{\\"type\\":\\"app_control\\",\\"command\\":\\"open\\",'
            b'\\"target\\":\\"browser\\",\\"args\\":{},'
            b'\\"description\\":\\"x\\",\\"requires_confirm\\":false}'
            b'\\n```"}\n\n'
        )

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_recovered",
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

    stub_core_client.chat_stream = fake_chat_stream
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저 켜서 연어장 찾아줘"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_stream = original_chat_stream
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "embedded action recovered by action classifier" in body
    assert "action_dispatch" in body
    assert "open_url" in body


def test_stream_uses_core_model_fallback_when_action_compiler_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_CORE_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: None,
    )
    original_chat_request = stub_core_client.chat_request

    def fake_chat_request(**kwargs):
        return {
            "request_id": "req-fallback",
            "route": "realtime",
            "provider_mode": "local",
            "provider_name": "stub-core",
            "model_name": "stub-model",
            "content": (
                '{"mode":"direct","goal":"search","confidence":0.9,'
                '"reason":"fallback","actions":[{"name":"browser.search",'
                '"args":{"query":"연어장"},"description":"브라우저에서 연어장 검색",'
                '"requires_confirm":false}]}'
            ),
        }

    class CompletedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_core_fallback",
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

    stub_core_client.chat_request = fake_chat_request
    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = CompletedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "브라우저 켜서 연어장 찾아줘"},
            headers={
                **auth_headers(),
                "x-client-enabled-capabilities": "browser.search,browser.navigate,browser.open",
                "x-client-search-engine": "naver",
            },
        )
    finally:
        stub_core_client.chat_request = original_chat_request
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "action_dispatch" in body
    assert "open_url" in body
    assert "search.naver.com" in body


def test_action_context_trims_large_application_list_to_message_mentions() -> None:
    from router.router import _trim_action_context_for_message

    context = {
        "available_applications": [
            "Google Chrome",
            "Sublime Text",
            *[f"App {index}" for index in range(40)],
        ],
        "available_application_names": [
            "Google Chrome",
            "Sublime Text",
            *[f"App {index}" for index in range(40)],
        ],
    }

    trimmed = _trim_action_context_for_message(
        context,
        "Sublime Text 열어서 안녕하세요 작성해줘",
    )

    assert trimmed is not None
    assert trimmed["available_applications"] == ["Sublime Text"]
    assert trimmed["available_application_names"] == ["Sublime Text"]


def test_local_app_type_request_opens_app_and_types_exact_text() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "sublimetext에 안녕하세요 작성해줘",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "available_applications": ["Sublime Text"],
            "available_application_names": ["Sublime Text"],
        },
    )

    assert decision is not None
    assert decision.should_act is True
    assert decision.execution_mode == "direct_sequence"
    assert decision.intent == "app.open+keyboard.type"
    assert [action.type for action in decision.actions] == ["app_control", "keyboard_type"]
    assert decision.actions[0].target == "Sublime Text"
    assert decision.actions[1].payload == "안녕하세요"


def test_local_app_type_request_strips_korean_quote_marker() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "sublimetext에 안녕하세요라고 작성해줘",
        context={
            "capabilities": ["app.open", "keyboard.type"],
            "available_applications": ["Sublime Text"],
            "available_application_names": ["Sublime Text"],
        },
    )

    assert decision is not None
    assert decision.actions[1].payload == "안녕하세요"


def test_action_context_trims_large_application_list_to_app_metadata_match() -> None:
    from router.router import _trim_action_context_for_message

    context = {
        "available_applications": [
            {
                "name": "Weather",
                "aliases": ["weather"],
                "capabilities": ["날씨", "forecast"],
            },
            *[{"name": f"App {index}"} for index in range(40)],
        ],
        "available_application_names": [
            "Weather",
            *[f"App {index}" for index in range(40)],
        ],
    }

    trimmed = _trim_action_context_for_message(context, "오늘 날씨 알려줘")

    assert trimmed is not None
    assert trimmed["available_applications"] == [
        {
            "name": "Weather",
            "aliases": ["weather"],
            "capabilities": ["날씨", "forecast"],
        }
    ]
    assert trimmed["available_application_names"] == ["Weather"]


def test_action_context_trims_string_app_list_using_local_alias_profile() -> None:
    from router.router import _trim_action_context_for_message

    context = {
        "available_applications": [
            "Weather",
            *[f"App {index}" for index in range(40)],
        ],
        "available_application_names": [
            "Weather",
            *[f"App {index}" for index in range(40)],
        ],
    }

    trimmed = _trim_action_context_for_message(context, "오늘 날씨 어때?")

    assert trimmed is not None
    assert trimmed["available_applications"] == ["Weather"]
    assert trimmed["available_application_names"] == ["Weather"]


def test_weather_question_opens_weather_app_first() -> None:
    from router.router import _local_direct_action_decision

    context = {
        "capabilities": ["app.open", "browser.search"],
        "available_applications": [
            {
                "name": "Weather",
                "aliases": ["weather", "날씨"],
                "bundle_id": "com.apple.weather",
                "capabilities": ["weather", "forecast", "예보"],
            }
        ],
    }

    decision = _local_direct_action_decision("오늘 날씨 알려줘", context=context)

    assert decision is not None
    assert decision.intent == "app.open"
    assert decision.actions[0].type == "app_control"
    assert decision.actions[0].command == "open"
    assert decision.actions[0].target == "Weather"


def test_weather_question_opens_weather_app_from_string_app_list() -> None:
    from router.router import _local_direct_action_decision, _trim_action_context_for_message

    context = {
        "capabilities": ["app.open", "browser.search"],
        "available_applications": [
            "Weather",
            *[f"App {index}" for index in range(40)],
        ],
        "available_application_names": [
            "Weather",
            *[f"App {index}" for index in range(40)],
        ],
    }
    trimmed = _trim_action_context_for_message(context, "오늘 날씨 어때?")

    decision = _local_direct_action_decision("오늘 날씨 어때?", context=trimmed)

    assert decision is not None
    assert decision.intent == "app.open"
    assert decision.actions[0].type == "app_control"
    assert decision.actions[0].command == "open"
    assert decision.actions[0].target == "Weather"


def test_weather_question_opens_weather_app_with_unrelated_browser_context() -> None:
    from router.router import _local_direct_action_decision

    context = {
        "capabilities": ["app.open", "browser.search"],
        "browser_active": True,
        "latest_action_result": {
            "action_type": "open_url",
            "target": "https://www.google.com/search?q=test",
        },
        "available_applications": [
            {
                "name": "Weather",
                "aliases": ["weather", "날씨"],
                "bundle_id": "com.apple.weather",
                "capabilities": ["weather", "forecast", "예보"],
            }
        ],
    }

    decision = _local_direct_action_decision("오늘 날씨 알려줘", context=context)

    assert decision is not None
    assert decision.intent == "app.open"
    assert decision.actions[0].type == "app_control"
    assert decision.actions[0].command == "open"
    assert decision.actions[0].target == "Weather"


def test_weather_question_does_not_reopen_active_weather_app() -> None:
    from router.router import _local_direct_action_decision

    context = {
        "capabilities": ["app.open", "browser.search"],
        "latest_action_result": {
            "active_app": "Weather",
            "bundle_id": "com.apple.weather",
        },
        "available_applications": [{"name": "Weather", "aliases": ["weather", "날씨"]}],
    }

    assert _local_direct_action_decision("대구 날씨 알려줘", context=context) is None


def test_weather_app_reopen_request_opens_active_weather_app() -> None:
    from router.router import _local_direct_action_decision

    context = {
        "capabilities": ["app.open", "browser.search"],
        "latest_action_result": {
            "active_app": "Weather",
            "bundle_id": "com.apple.weather",
        },
        "available_applications": [
            {
                "name": "Weather",
                "aliases": ["weather", "날씨"],
                "bundle_id": "com.apple.weather",
                "capabilities": ["weather", "forecast"],
            }
        ],
    }

    decision = _local_direct_action_decision("날씨앱 다시 켜줘", context=context)

    assert decision is not None
    assert decision.intent == "app.open"
    assert decision.actions[0].type == "app_control"
    assert decision.actions[0].command == "open"
    assert decision.actions[0].target == "Weather"


def test_weather_app_open_request_opens_active_weather_app() -> None:
    from router.router import _local_direct_action_decision

    context = {
        "capabilities": ["app.open", "browser.search"],
        "working_context": {
            "active_app": "Weather",
            "bundle_id": "com.apple.weather",
        },
        "available_applications": [{"name": "Weather", "aliases": ["weather", "날씨"]}],
    }

    decision = _local_direct_action_decision("날씨 앱 열어줘", context=context)

    assert decision is not None
    assert decision.intent == "app.open"
    assert decision.actions[0].target == "Weather"


def test_korean_stocks_app_request_opens_runtime_stocks_app() -> None:
    from router.router import _local_direct_action_decision, _runtime_applications_for_context

    applications = _runtime_applications_for_context(
        [
            {
                "name": "Stocks",
                "display_name": "Stocks",
                "bundle_id": "com.apple.stocks",
                "aliases": ["Stocks", "stocks"],
                "kind": "macos_app",
            }
        ]
    )
    context = {
        "capabilities": ["app.open", "browser.search"],
        "available_applications": applications,
    }

    decision = _local_direct_action_decision("주식앱 켜줄래?", context=context)

    assert decision is not None
    assert decision.intent == "app.open"
    assert decision.actions[0].type == "app_control"
    assert decision.actions[0].command == "open"
    assert decision.actions[0].target == "Stocks"


def test_local_laptop_app_reference_opens_runtime_stocks_app() -> None:
    from router.router import _local_direct_action_decision, _runtime_applications_for_context

    applications = _runtime_applications_for_context(
        [
            {
                "name": "Stocks",
                "display_name": "Stocks",
                "bundle_id": "com.apple.stocks",
                "aliases": ["Stocks", "stocks"],
                "kind": "macos_app",
            }
        ]
    )
    context = {
        "capabilities": ["app.open", "browser.search"],
        "available_applications": applications,
    }

    decision = _local_direct_action_decision("내 노트북의 주식앱", context=context)

    assert decision is not None
    assert decision.intent == "app.open"
    assert decision.actions[0].target == "Stocks"


def test_action_context_trims_stocks_app_by_korean_runtime_alias() -> None:
    from router.router import _runtime_applications_for_context, _trim_action_context_for_message

    applications = _runtime_applications_for_context(
        [
            {
                "name": "Stocks",
                "display_name": "Stocks",
                "bundle_id": "com.apple.stocks",
                "aliases": ["Stocks", "stocks"],
                "kind": "macos_app",
            },
            *[{"name": f"App {index}"} for index in range(40)],
        ]
    )
    context = {
        "available_applications": applications,
        "available_application_names": [app["name"] for app in applications],
    }

    trimmed = _trim_action_context_for_message(context, "주식앱 켜줄래?")

    assert trimmed is not None
    assert trimmed["available_application_names"] == ["Stocks"]
    assert trimmed["available_applications"][0]["name"] == "Stocks"


def test_runtime_application_request_preserves_routing_metadata() -> None:
    from router.router import RuntimeApplicationRequest

    app = RuntimeApplicationRequest.model_validate(
        {
            "name": "Stocks",
            "aliases": ["주식"],
            "capabilities": ["stocks"],
            "categories": ["finance"],
            "keywords": ["주식 시세"],
        }
    )

    dumped = app.model_dump()

    assert dumped["aliases"] == ["주식"]
    assert dumped["capabilities"] == ["stocks"]
    assert dumped["categories"] == ["finance"]
    assert dumped["keywords"] == ["주식 시세"]


def test_runtime_terminal_request_preserves_policy_metadata() -> None:
    from router.router import TerminalProfileRequest

    terminal = TerminalProfileRequest.model_validate(
        {
            "enabled": True,
            "shell": "zsh",
            "cwd": "/Users/chawonje/Desktop/Workspace/project/JARVIS",
            "allowed_commands": ["echo", "pwd", "ls", "git status"],
            "allowed_cwds": ["/Users/chawonje/Desktop/Workspace/project/JARVIS"],
            "timeout_seconds": 20,
        }
    )

    dumped = terminal.model_dump()

    assert dumped["enabled"] is True
    assert dumped["allowed_commands"] == ["echo", "pwd", "ls", "git status"]
    assert dumped["allowed_cwds"] == ["/Users/chawonje/Desktop/Workspace/project/JARVIS"]


def test_terminal_run_request_dispatches_confirmed_terminal_action() -> None:
    from router.router import _local_direct_action_decision

    context = {
        "capabilities": ["terminal.run"],
        "terminal": {
            "enabled": True,
            "shell": "zsh",
            "cwd": "/Users/chawonje/Desktop/Workspace/project/JARVIS",
            "allowed_commands": ["echo", "pwd", "ls", "git status"],
            "allowed_cwds": ["/Users/chawonje/Desktop/Workspace/project/JARVIS"],
            "timeout_seconds": 20,
        },
    }

    decision = _local_direct_action_decision(
        "터미널에서 git status 실행해줘",
        context=context,
    )

    assert decision is not None
    assert decision.intent == "terminal.run"
    action = decision.actions[0]
    assert action.type == "terminal"
    assert action.command == "execute"
    assert action.payload == "git status"
    assert action.args["command"] == "git status"
    assert action.args["cwd"] == "/Users/chawonje/Desktop/Workspace/project/JARVIS"
    assert action.args["timeout"] == 20
    assert action.requires_confirm is True


def test_terminal_run_request_accepts_cmd_alias_for_pwd() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "CMD에서 PWD 수행해줘",
        context={
            "capabilities": ["terminal.run"],
            "terminal": {"enabled": True, "allowed_commands": ["pwd"]},
        },
    )

    assert decision is not None
    assert decision.intent == "terminal.run"
    assert decision.actions[0].payload == "PWD"
    assert decision.actions[0].args["command"] == "PWD"


def test_browser_search_with_cmd_query_is_not_terminal_command() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "브라우저에서 cmd에 대해 검색해줘",
        context={
            "capabilities": ["terminal.run", "open_url", "browser.search"],
            "terminal": {
                "enabled": True,
                "shell": "zsh",
                "cwd": "/Users/chawonje/Desktop/Workspace/project/JARVIS",
                "allowed_commands": ["pwd"],
            },
        },
    )

    assert decision is not None
    assert decision.intent == "browser.search"
    action = decision.actions[0]
    assert action.type == "open_url"
    assert action.args["query"] == "cmd"
    assert action.target is not None
    assert "q=cmd" in action.target


def test_browser_search_strips_trailing_browser_open_framing() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "매콤한 소불고기 레시피 브라우저 켜서 찾아줘",
        context={"capabilities": ["open_url", "browser.search", "browser.open"]},
    )

    assert decision is not None
    assert decision.intent == "browser.search"
    action = decision.actions[0]
    assert action.type == "open_url"
    assert action.args["query"] == "매콤한 소불고기 레시피"
    assert action.target is not None
    assert "%EB%A7%A4%EC%BD%A4%ED%95%9C" in action.target


def test_explicit_search_without_browser_word_dispatches_browser_search() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "OpenAI 최신 소식 검색해줘",
        context={"capabilities": ["open_url", "browser.search"]},
    )

    assert decision is not None
    assert decision.intent == "browser.search"
    action = decision.actions[0]
    assert action.type == "open_url"
    assert action.args["query"] == "OpenAI 최신 소식"
    assert action.target is not None
    assert "OpenAI" in action.target


def test_explicit_search_strips_trailing_topic_relation() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "창원대학교 박동규 교수님애 대해서 검색해줘",
        context={"capabilities": ["open_url", "browser.search"]},
    )

    assert decision is not None
    assert decision.intent == "browser.search"
    action = decision.actions[0]
    assert action.type == "open_url"
    assert action.args["query"] == "창원대학교 박동규 교수님"
    assert action.target is not None
    assert "%EC%B0%BD%EC%9B%90%EB%8C%80%ED%95%99%EA%B5%90" in action.target


def test_browser_search_result_selection_beats_search_template() -> None:
    from router.router import _local_direct_action_decision

    for context in (
        {
            "browser_active": True,
            "last_query": "openai",
            "last_url": "https://www.google.com/search?q=openai",
            "capabilities": ["open_url", "browser.search", "browser.select_result"],
        },
        {"capabilities": ["open_url", "browser.search", "browser.select_result"]},
        None,
    ):
        decision = _local_direct_action_decision(
            "첫번째 검색결과 들어가 줄래?",
            context=context,
        )

        assert decision is not None
        assert decision.intent == "browser.select_result"
        action = decision.actions[0]
        assert action.type == "browser_control"
        assert action.command == "select_result"
        assert action.args["index"] == 1


def test_browser_open_only_does_not_search_polite_suffix() -> None:
    from router.router import _browser_search_query_from_message, _local_direct_action_decision

    decision = _local_direct_action_decision(
        "브라우저 열어줘",
        context={"capabilities": ["browser.open", "browser.search", "open_url"]},
    )

    assert decision is not None
    assert decision.intent == "browser.open"
    action = decision.actions[0]
    assert action.type == "browser"
    assert action.command == "open"
    assert _browser_search_query_from_message("브라우저 열어줘", context=None) is None


def test_current_browser_tab_close_dispatches_browser_control() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "지금 열려있는 탭 닫아줘",
        context={"capabilities": ["browser.close_tab", "browser_control"]},
    )

    assert decision is not None
    assert decision.intent == "browser.close_tab"
    action = decision.actions[0]
    assert action.type == "browser_control"
    assert action.command == "close_tab"
    assert action.target == "active_tab"
    assert action.args["browser"] == "chrome"


def test_bare_browser_search_without_previous_topic_is_not_dispatched() -> None:
    from router.router import _local_direct_action_decision

    context = {"capabilities": ["open_url", "browser.search"]}

    assert _local_direct_action_decision("브라우저에서 검색해줘", context=context) is None
    assert _local_direct_action_decision("검색해줘", context=context) is None


def test_action_architecture_question_is_not_terminal_action() -> None:
    from planner.conversation_routing import ConversationContext, evaluate_conversation_mode
    from router.router import _local_direct_action_decision

    message = (
        "현재 앱 실행, 브라우저 검색, 터미널 실행 액션이 충돌하지 않도록 "
        "라우팅 우선순위를 설계해줘"
    )

    assert _local_direct_action_decision(
        message,
        context={"capabilities": ["terminal.run", "browser.search"]},
    ) is None
    decision = evaluate_conversation_mode(message, context=ConversationContext())
    assert decision.mode.value == "deep"


def test_calendar_todo_architecture_question_routes_deep() -> None:
    from planner.conversation_routing import ConversationContext, evaluate_conversation_mode

    decision = evaluate_conversation_mode(
        (
            "todo 기능을 구글 캘린더 연동까지 고려해서 "
            "백엔드/프론트/DB/API 설계로 나눠서 제안해줘"
        ),
        context=ConversationContext(),
    )

    assert decision.mode.value == "deep"


def test_stream_action_architecture_question_uses_deep_chat_without_action_gate() -> None:
    stub_core_client.last_chat_stream_request = None
    message = (
        "앱 실행, 브라우저 검색, 터미널 실행 액션이 충돌하지 않도록 "
        "라우팅 우선순위를 설계해줘"
    )

    response = client.post(
        "/conversation/stream",
        json={"message": message},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    events = _collect_events(response.text)
    event_names = [event for event, _payload in events]
    assert "classification" in event_names
    assert "plan_summary" not in event_names
    assert "action_intent" not in event_names
    assert "action_dispatch" not in event_names
    assert stub_core_client.last_chat_stream_request is not None
    assert stub_core_client.last_chat_stream_request["route_override"] == "deep"
    assert stub_core_client.last_chat_stream_request["task_type"] == "analysis"


def test_stream_calendar_todo_architecture_question_uses_deep_chat() -> None:
    stub_core_client.last_chat_stream_request = None
    message = (
        "todo 기능을 구글 캘린더 연동까지 고려해서 "
        "백엔드/프론트/DB/API 설계로 나눠서 제안해줘"
    )

    response = client.post(
        "/conversation/stream",
        json={"message": message},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    events = _collect_events(response.text)
    event_names = [event for event, _payload in events]
    assert "classification" in event_names
    assert "plan_summary" not in event_names
    assert "action_dispatch" not in event_names
    assert stub_core_client.last_chat_stream_request is not None
    assert stub_core_client.last_chat_stream_request["route_override"] == "deep"
    assert stub_core_client.last_chat_stream_request["task_type"] == "analysis"


def test_stream_code_output_request_uses_deep_chat() -> None:
    stub_core_client.last_chat_stream_request = None
    message = "FastAPI에서 SSE 스트림 보내는 예제 코드 제공해줘"

    response = client.post(
        "/conversation/stream",
        json={"message": message},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    events = _collect_events(response.text)
    event_names = [event for event, _payload in events]
    assert "classification" in event_names
    assert "action_dispatch" not in event_names
    assert stub_core_client.last_chat_stream_request is not None
    assert stub_core_client.last_chat_stream_request["route_override"] == "deep"
    assert stub_core_client.last_chat_stream_request["task_type"] == "analysis"


def test_stream_without_leading_action_ack_removes_split_ack_prefix() -> None:
    from router.router import _stream_without_leading_action_ack

    chunks = iter(
        [
            b'event: assistant_delta\ndata: {"content":"\xec\xa7\x84"}\n\n',
            b'event: assistant_delta\ndata: {"content":"\xed\x96\x89"}\n\n',
            (
                b'event: assistant_delta\ndata: {"content":"'
                b'\xed\x95\x98\xea\xb2\xa0\xec\x8a\xb5\xeb\x8b\x88'
                b'\xeb\x8b\xa4!"}\n\n'
            ),
            b'event: assistant_delta\ndata: {"content":" 1. design"}\n\n',
        ]
    )

    body = b"".join(_stream_without_leading_action_ack(chunks)).decode()

    assert "진행하겠습니다" not in body
    assert "1. design" in body


def test_browser_search_followup_uses_previous_user_message_topic() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "브라우저에서 찾아줘",
        context={
            "capabilities": ["open_url", "browser.search"],
            "previous_user_message": {"text": "남은 반찬 처리하는법 찾아줘"},
        },
    )

    assert decision is not None
    assert decision.intent == "browser.search"
    action = decision.actions[0]
    assert action.type == "open_url"
    assert action.args["query"] == "남은 반찬 처리하는법"
    assert action.target is not None
    assert "%EB%82%A8%EC%9D%80+%EB%B0%98%EC%B0%AC" in action.target


def test_stream_browser_search_followup_uses_last_turn_topic() -> None:
    client.app.state.recent_user_messages = {}

    first_response = client.post(
        "/conversation/stream",
        json={"message": "남은 반찬 처리하는법 찾아줘"},
        headers=auth_headers(),
    )
    assert first_response.status_code == 200

    followup_response = client.post(
        "/conversation/stream",
        json={"message": "브라우저에서 찾아줘"},
        headers=auth_headers(),
    )

    assert followup_response.status_code == 200
    body = followup_response.text
    assert '"intent": "browser.search"' in body
    assert '"query": "남은 반찬 처리하는법"' in body
    assert "%EB%82%A8%EC%9D%80+%EB%B0%98%EC%B0%AC" in body


def test_stream_previous_question_recall_uses_last_user_message() -> None:
    client.app.state.recent_user_messages = {}

    first_response = client.post(
        "/conversation/stream",
        json={"message": "남은 반찬 처리하는법 찾아줘"},
        headers=auth_headers(),
    )
    assert first_response.status_code == 200
    stub_core_client.last_chat_stream_request = None

    recall_response = client.post(
        "/conversation/stream",
        json={"message": "이전 질문 내가 뭐라했어?"},
        headers=auth_headers(),
    )

    assert recall_response.status_code == 200
    body = recall_response.text
    assert "이전 질문은" in body
    assert "남은 반찬 처리하는법 찾아줘" in body
    assert "local previous user message recall" in body
    assert stub_core_client.last_chat_stream_request is None


def test_terminal_run_request_dispatches_explicit_command_for_client_policy() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "터미널에서 ssh breakpack@Mymacmini 실행해줘",
        context={
            "capabilities": ["terminal.run"],
            "terminal": {"enabled": True, "allowed_commands": ["pwd"]},
        },
    )

    assert decision is not None
    assert decision.intent == "terminal.run"
    action = decision.actions[0]
    assert action.payload == "ssh breakpack@Mymacmini"
    assert action.args["command"] == "ssh breakpack@Mymacmini"
    assert action.requires_confirm is True


def test_terminal_run_request_respects_disabled_terminal_context() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "터미널에서 pwd 실행해줘",
        context={
            "capabilities": ["terminal.run"],
            "terminal": {"enabled": False, "allowed_commands": ["pwd"]},
        },
    )

    assert decision is None


def test_todo_create_request_dispatches_server_todo_action() -> None:
    stub_core_client.last_todo_request = None

    response = client.post(
        "/conversation/stream",
        json={"message": "컴파일러 과제 5/18 23:59 까지 할일에 추가해 줄래?"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert '"intent": "todo.create"' in body
    assert "event: action_dispatch" not in body
    assert "event: action_result" in body
    assert '"source": "server_todo"' in body
    assert "요청한 작업을 실행했습니다." in body
    assert stub_core_client.last_todo_request is not None
    assert stub_core_client.last_todo_request["method"] == "create"
    assert stub_core_client.last_todo_request["user_id"] == "u1"
    todo_body = stub_core_client.last_todo_request["body"]
    assert isinstance(todo_body, dict)
    assert todo_body["title"] == "컴파일러 과제"
    assert str(todo_body["due_at"]).endswith("-05-18T23:59:00+09:00")
    assert todo_body["timezone"] == "Asia/Seoul"


def test_todo_create_request_extracts_relative_day_time_and_clean_title() -> None:
    stub_core_client.last_todo_request = None

    response = client.post(
        "/conversation/stream",
        json={"message": "할일 목록에 오늘 오후 8시 미팅 추가해줘"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert '"intent": "todo.create"' in body
    assert '"title": "미팅"' in body
    assert stub_core_client.last_todo_request is not None
    todo_body = stub_core_client.last_todo_request["body"]
    assert isinstance(todo_body, dict)
    assert todo_body["title"] == "미팅"
    assert str(todo_body["due_at"]).endswith("T20:00:00+09:00")


def test_todo_list_request_dispatches_server_todo_list_action() -> None:
    stub_core_client.last_todo_request = None

    response = client.post(
        "/conversation/stream",
        json={"message": "남은 할일 뭐남았어?"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert '"intent": "todo.list"' in body
    assert "event: action_dispatch" not in body
    assert "event: action_result" in body
    assert '"source": "server_todo"' in body
    assert "남은 할 일입니다." in body
    assert "테스트 todo" in body
    assert stub_core_client.last_todo_request == {
        "method": "list",
        "user_id": "u1",
        "status": "open",
        "include_deleted": False,
        "limit": 50,
    }


def test_todo_today_list_filters_server_results(monkeypatch) -> None:
    today = datetime.now(ZoneInfo("Asia/Seoul"))
    tomorrow = today + timedelta(days=1)

    def fake_list_todos(
        *,
        user_id: str,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> dict[str, object]:
        return {
            "items": [
                {
                    "id": "todo-today",
                    "user_id": user_id,
                    "title": "오늘 회의",
                    "status": status or "open",
                    "due_at": today.isoformat(),
                },
                {
                    "id": "todo-future",
                    "user_id": user_id,
                    "title": "내일 회의",
                    "status": status or "open",
                    "due_at": tomorrow.isoformat(),
                },
            ]
        }

    monkeypatch.setattr(stub_core_client, "list_todos", fake_list_todos)

    for message in ("오늘 할일 알려줄래", "할일 목록 리스트업해줄래"):
        response = client.post(
            "/conversation/stream",
            json={"message": message},
            headers=auth_headers(),
        )

        assert response.status_code == 200
        body = response.text
        assert '"intent": "todo.list"' in body
        assert "오늘 회의" in body
        assert f"오늘 {today:%H:%M}" in body
        assert today.isoformat() not in body
        if "오늘" in message:
            assert "내일 회의" not in body


def test_free_time_check_lists_today_todos_and_summarizes_slots(monkeypatch) -> None:
    today = datetime.now(ZoneInfo("Asia/Seoul"))

    def fake_list_todos(
        *,
        user_id: str,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> dict[str, object]:
        return {
            "items": [
                {
                    "id": "todo-morning",
                    "user_id": user_id,
                    "title": "오전 회의",
                    "status": status or "open",
                    "due_at": today.replace(hour=10, minute=0, second=0).isoformat(),
                },
                {
                    "id": "todo-afternoon",
                    "user_id": user_id,
                    "title": "오후 리뷰",
                    "status": status or "open",
                    "due_at": today.replace(hour=15, minute=0, second=0).isoformat(),
                },
            ]
        }

    monkeypatch.setattr(stub_core_client, "list_todos", fake_list_todos)

    response = client.post(
        "/conversation/stream",
        json={"message": "오늘 빈시간 체크해줘"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert '"intent": "todo.list"' in body
    assert '"summary_mode": "free_time"' in body
    assert "오늘 할 일 목록 기준 빈 시간입니다." in body
    assert "09:00-10:00" in body
    assert "11:00-15:00" in body
    assert "16:00-18:00" in body


def test_free_time_check_reports_result_after_empty_todo_action(monkeypatch) -> None:
    def fake_list_todos(
        *,
        user_id: str,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> dict[str, object]:
        return {"items": []}

    monkeypatch.setattr(stub_core_client, "list_todos", fake_list_todos)

    response = client.post(
        "/conversation/stream",
        json={"message": "오늘 빈시간 알려줘"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert "진행하겠습니다!" not in body
    assert body.index("event: action_result") < body.index(
        "오늘 등록된 시간 지정 할 일이 없습니다."
    )
    events = _collect_events(body)
    done_payloads = [
        payload for event_name, payload in events if event_name == "assistant_done"
    ]
    assert done_payloads[-1]["content"] == (
        "오늘 등록된 시간 지정 할 일이 없습니다. 09:00-18:00 전체가 비어 있습니다."
    )


def test_todo_delete_request_deletes_single_matching_server_todo(monkeypatch) -> None:
    today = datetime.now(ZoneInfo("Asia/Seoul")).replace(
        hour=18,
        minute=0,
        second=0,
        microsecond=0,
    )
    calls: list[tuple[str, object]] = []

    def fake_list_todos(
        *,
        user_id: str,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> dict[str, object]:
        calls.append(("list", {"user_id": user_id, "status": status, "limit": limit}))
        return {
            "items": [
                {
                    "id": "todo-meeting",
                    "user_id": user_id,
                    "title": "미팅",
                    "status": status or "open",
                    "due_at": today.isoformat(),
                },
                {
                    "id": "todo-other",
                    "user_id": user_id,
                    "title": "다른 할 일",
                    "status": status or "open",
                    "due_at": today.replace(hour=21).isoformat(),
                },
            ]
        }

    def fake_delete_todo(*, user_id: str, todo_id: str) -> dict[str, object]:
        calls.append(("delete", todo_id))
        return {"id": todo_id, "deleted": True}

    monkeypatch.setattr(stub_core_client, "list_todos", fake_list_todos)
    monkeypatch.setattr(stub_core_client, "delete_todo", fake_delete_todo)

    response = client.post(
        "/conversation/stream",
        json={"message": "오늘 6시 미팅 삭제해줘"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert '"intent": "todo.delete"' in body
    assert "event: action_dispatch" not in body
    assert '"source": "server_todo"' in body
    assert "요청한 작업을 실행했습니다." in body
    assert ("delete", "todo-meeting") in calls


def test_todo_remove_synonym_routes_to_delete_before_list() -> None:
    from router.router import _local_direct_action_decision

    decision = _local_direct_action_decision(
        "오후 5시 미팅 할일 목록에서 없애줘",
        context={},
    )

    assert decision is not None
    assert decision.intent == "todo.delete"
    action = decision.actions[0]
    assert action.command == "delete"
    assert action.args["query"] == "미팅"
    assert action.args["due_hours"] == [17]


def test_runtime_profile_llm_enrichment_adds_app_aliases(monkeypatch) -> None:
    from planner.runtime_profile_enricher import enrich_runtime_profile_applications

    monkeypatch.setenv("JARVIS_RUNTIME_PROFILE_LLM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("JARVIS_RUNTIME_PROFILE_LLM_CHUNK_SIZE", "10")
    calls: list[dict[str, object]] = []

    def fake_complete_model_text(**kwargs):
        calls.append(kwargs)
        return (
            '{"applications":[{"index":0,"aliases":["계산기"],'
            '"capabilities":["calculator"],"categories":["utility"],'
            '"keywords":["계산"]}]}'
        )

    monkeypatch.setattr(
        "planner.runtime_profile_enricher.complete_model_text",
        fake_complete_model_text,
    )

    profile = enrich_runtime_profile_applications(
        {
            "platform": "macos",
            "applications": [
                {
                    "name": "Calculator",
                    "display_name": "Calculator",
                    "bundle_id": "com.apple.calculator",
                    "aliases": ["Calculator"],
                }
            ],
            "metadata": {},
        }
    )

    app = profile["applications"][0]
    assert "계산기" in app["aliases"]
    assert "calculator" in app["capabilities"]
    assert profile["metadata"]["app_enrichment"]["source"] == "llm"
    assert calls[0]["payload"]["think"] is False


def test_runtime_profile_llm_enrichment_accepts_nested_list_response(
    monkeypatch,
) -> None:
    from planner.runtime_profile_enricher import enrich_runtime_profile_applications

    monkeypatch.setenv("JARVIS_RUNTIME_PROFILE_LLM_ENRICH_ENABLED", "1")

    def fake_complete_model_text(**kwargs):
        return (
            "["
            '{"applications":[{"index":0,"aliases":["계산기"],'
            '"capabilities":["calculator"]}]},'
            '{"applications":[{"index":1,"aliases":["메모"],'
            '"capabilities":["notes"]}]}'
            "]"
        )

    monkeypatch.setattr(
        "planner.runtime_profile_enricher.complete_model_text",
        fake_complete_model_text,
    )

    profile = enrich_runtime_profile_applications(
        {
            "platform": "macos",
            "applications": [
                {"name": "Calculator", "aliases": ["Calculator"]},
                {"name": "Notes", "aliases": ["Notes"]},
            ],
            "metadata": {},
        }
    )

    assert "계산기" in profile["applications"][0]["aliases"]
    assert "메모" in profile["applications"][1]["aliases"]
    assert profile["metadata"]["app_enrichment"]["llm_succeeded"] is True


def test_runtime_profile_enrichment_keeps_builtin_alias_when_llm_disabled(
    monkeypatch,
) -> None:
    from planner.runtime_profile_enricher import enrich_runtime_profile_applications

    monkeypatch.setenv("JARVIS_RUNTIME_PROFILE_LLM_ENRICH_ENABLED", "0")

    profile = enrich_runtime_profile_applications(
        {
            "platform": "macos",
            "applications": [
                {
                    "name": "Stocks",
                    "display_name": "Stocks",
                    "bundle_id": "com.apple.stocks",
                    "aliases": ["Stocks", "stocks"],
                }
            ],
            "metadata": {},
        }
    )

    app = profile["applications"][0]
    assert "주식" in app["aliases"]
    assert "finance" in app["capabilities"]
    assert profile["metadata"]["app_enrichment"]["source"] == "builtin"


def test_stream_can_disable_core_action_fallback(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ACTION_INTENT_CORE_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: None,
    )
    original_chat_request = stub_core_client.chat_request
    calls = 0

    def fake_chat_request(**kwargs):
        nonlocal calls
        calls += 1
        return original_chat_request(**kwargs)

    stub_core_client.chat_request = fake_chat_request
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "다시 대답해봐"},
            headers=auth_headers(),
        )
    finally:
        stub_core_client.chat_request = original_chat_request

    assert response.status_code == 200
    assert calls == 0
    assert "event: assistant_delta" in response.text
    assert "compiler_unavailable" in response.text


def test_stream_failed_client_action_does_not_emit_success(monkeypatch) -> None:
    from planner.action_intent_classifier import ActionIntentDecision

    action = ClientAction(
        type="open_url",
        command=None,
        target="https://example.com",
        args={},
        description="open example",
        requires_confirm=False,
    )

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        lambda *args, **kwargs: ActionIntentDecision(
            should_act=True,
            execution_mode="direct",
            intent="browser",
            confidence=0.9,
            reason="test action",
            actions=[action],
        ),
    )

    class FailedDispatcher:
        context_store = None

        def enqueue(self, *, user_id, request_id, action):
            return ClientActionEnvelope(
                action_id="act_failed",
                request_id=request_id,
                action=action,
            )

        def wait_for_result(self, *, action_id, request_id, timeout_seconds=None):
            return ClientActionResult(
                action_id=action_id,
                request_id=request_id,
                status="failed",
                error="client failed",
            )

    original_dispatcher = client.app.state.action_dispatcher
    client.app.state.action_dispatcher = FailedDispatcher()
    try:
        response = client.post(
            "/conversation/stream",
            json={"message": "open example"},
            headers=auth_headers(),
        )
    finally:
        client.app.state.action_dispatcher = original_dispatcher

    assert response.status_code == 200
    body = response.text
    assert "요청한 작업을 실행했습니다." not in body
    assert "클라이언트 액션 실행에 실패했습니다" in body
    assert "client failed" in body


def test_verify_mock_success() -> None:
    response = client.post(
        "/verify",
        json={
            "request_id": "r2",
            "check": "text",
            "expected": "ok",
            "actual": "ok",
            "contract_version": "1.0",
        },
        headers=auth_headers(),
    )
    assert response.status_code == 200
    assert response.json()["passed"] is True


def test_protected_endpoint_requires_token() -> None:
    response = client.post("/conversation/respond", json={"message": "hello"})

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTH_REQUIRED"


def test_signup_is_public() -> None:
    response = client.post(
        "/auth/signup",
        json={
            "email": "new-user@example.com",
            "name": "New User",
            "password": "secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "u2"


def test_pending_action_poll_access_log_filter_suppresses_success() -> None:
    access_filter = SuppressPendingActionPollAccessLog()
    pending_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:51235", "GET", "/client/actions/pending?limit=20", "1.1", 200),
        exc_info=None,
    )
    error_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:51235", "GET", "/client/actions/pending?limit=20", "1.1", 500),
        exc_info=None,
    )

    assert access_filter.filter(pending_record) is False
    assert access_filter.filter(error_record) is True
