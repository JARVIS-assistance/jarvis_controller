from fastapi.testclient import TestClient

from jarvis_controller.app import app


client = TestClient(app)


def test_execute_unsupported_action_returns_standard_error() -> None:
    response = client.post(
        "/execute",
        json={
            "request_id": "r3",
            "action": "drag",
            "target": "#x",
            "contract_version": "1.0",
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error_code"] == "UNSUPPORTED_ACTION"
    assert payload["contract_version"] == "1.0"
