from fastapi.testclient import TestClient
from jarvis_contracts import JarvisCoreEndpoints

from jarvis_controller.app import create_app
from jarvis_controller.middleware.core_client import CoreResponse
from jarvis_controller.middleware.gateway_client import GatewayPrincipal


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


def test_conversation_stream_emits_thinking_and_plan_for_deep_query() -> None:
    response = client.post(
        "/conversation/stream",
        json={"message": "이 에러 로그 원인 깊게 분석해줘\nTraceback: boom"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.text
    assert "event: classification" in body
    assert '"category": "deep"' in body
    assert "event: thinking" in body
    assert "DeepThinking..." in body
    assert "event: plan_step" in body
    assert stub_core_client.last_chat_stream_request is not None
    assert stub_core_client.last_chat_stream_request["route_override"] == "deep"


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
