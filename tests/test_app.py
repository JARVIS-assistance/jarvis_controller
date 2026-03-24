from fastapi.testclient import TestClient

from jarvis_controller.app import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_execute_mock_success() -> None:
    response = client.post(
        "/execute",
        json={
            "request_id": "r1",
            "action": "click",
            "target": "#submit",
            "contract_version": "1.0",
        },
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
    )
    assert response.status_code == 200
    assert response.json()["passed"] is True
