from fastapi.testclient import TestClient
from jarvis_controller.app import create_app
from jarvis_controller.middleware.gateway_client import GatewayPrincipal


class StubGatewayClient:
    def validate_token(self, token: str, **kwargs) -> GatewayPrincipal:
        return GatewayPrincipal(user_id="u1", active=True)


client = TestClient(create_app(gateway_client=StubGatewayClient()))


def test_execute_unsupported_action_returns_standard_error() -> None:
    response = client.post(
        "/execute",
        json={
            "request_id": "r3",
            "action": "drag",
            "target": "#x",
            "contract_version": "1.0",
        },
        headers={"Authorization": "Bearer token-123"},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error_code"] == "UNSUPPORTED_ACTION"
    assert payload["contract_version"] == "1.0"
