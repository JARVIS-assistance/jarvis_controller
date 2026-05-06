import time

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


def test_realtime_stream_does_not_wait_for_slow_action_classifier(monkeypatch) -> None:
    def slow_no_action(*args, **kwargs):
        time.sleep(0.4)
        return None

    monkeypatch.setattr(
        "router.router.classify_client_action_intent_decision",
        slow_no_action,
    )
    monkeypatch.setenv("JARVIS_ACTION_ARBITRATION_BUFFER_SECONDS", "0.01")

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


def test_fast_direct_action_arbitrates_before_realtime_text(monkeypatch) -> None:
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
    assert "event: assistant_delta" not in body


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
