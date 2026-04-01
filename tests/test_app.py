from fastapi.testclient import TestClient

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
            "tenant_id": "t1",
            "role": "admin",
        }

    def logout(self, token: str, **kwargs) -> dict[str, object]:
        assert token == "token-123"
        return {"ok": True}

    def validate_token(self, token: str, **kwargs) -> GatewayPrincipal:
        if token != "token-123":
            raise Exception("invalid or expired token")
        return GatewayPrincipal(user_id="u1", tenant_id="t1", role="admin", active=True)


class StubCoreClient:
    def run_realtime_conversation(self, message: str) -> CoreResponse:
        return CoreResponse(
            mode="realtime",
            summary="stub realtime",
            content=f"실시간 응답: {message}",
            next_actions=["noop"],
        )

    def run_deep_thinking(self, message: str) -> CoreResponse:
        return CoreResponse(
            mode="deep",
            summary="stub deep",
            content=f"Deep thinking result: {message}",
            next_actions=["inspect"],
        )


client = TestClient(
    create_app(gateway_client=StubGatewayClient(), core_client=StubCoreClient())
)


def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer token-123", "x-client-id": "test-client"}


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_proxies_to_gateway() -> None:
    response = client.post(
        "/auth/login",
        json={"username": "admin", "password": "admin123"},
        headers={"x-client-id": "test-client"},
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == "token-123"


def test_auth_me_uses_gateway_validation() -> None:
    response = client.get("/auth/me", headers=auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "u1"
    assert payload["tenant_id"] == "t1"


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
