from __future__ import annotations

import json

from jarvis_controller.middleware.core_client import CoreClient


class _LineResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)
        self.closed = False

    def readline(self) -> bytes:
        return next(self._lines, b"")

    def close(self) -> None:
        self.closed = True


class _JsonResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_chat_stream_yields_complete_sse_events(monkeypatch) -> None:
    response = _LineResponse(
        [
            b"event: meta\n",
            b'data: {"type":"meta"}\n',
            b"\n",
            b"event: assistant_delta\n",
            b'data: {"content":"hi"}\n',
            b"\n",
        ]
    )

    monkeypatch.setattr(
        "jarvis_controller.middleware.core_client.urllib.request.urlopen",
        lambda *args, **kwargs: response,
    )

    chunks = list(
        CoreClient(base_url="http://core").chat_stream(
            message="hello",
            user_id="u1",
            request_id="r1",
        )
    )

    assert chunks == [
        b'event: meta\ndata: {"type":"meta"}\n\n',
        b'event: assistant_delta\ndata: {"content":"hi"}\n\n',
    ]
    assert response.closed is True


def test_todo_methods_use_internal_todo_endpoints(monkeypatch) -> None:
    requests = []

    def fake_urlopen(request, **kwargs):
        requests.append(request)
        return _JsonResponse(b'{"items":[]}')

    monkeypatch.setattr(
        "jarvis_controller.middleware.core_client.urllib.request.urlopen",
        fake_urlopen,
    )

    client = CoreClient(base_url="http://core")
    result = client.list_todos(
        user_id="u1",
        status="open",
        include_deleted=True,
        limit=10,
    )

    assert result == {"items": []}
    request = requests[0]
    assert request.get_method() == "GET"
    assert request.full_url == (
        "http://core/internal/todos?include_deleted=true&limit=10&status=open"
    )
    assert request.headers["X-user-id"] == "u1"

    client.create_todo(user_id="u1", body={"title": "테스트"})
    request = requests[1]
    assert request.get_method() == "POST"
    assert request.full_url == "http://core/internal/todos"
    assert json.loads(request.data.decode("utf-8")) == {"title": "테스트"}

    client.update_todo(user_id="u1", todo_id="todo-1", body={"status": "completed"})
    request = requests[2]
    assert request.get_method() == "PATCH"
    assert request.full_url == "http://core/internal/todos/todo-1"
    assert json.loads(request.data.decode("utf-8")) == {"status": "completed"}

    client.delete_todo(user_id="u1", todo_id="todo-1")
    request = requests[3]
    assert request.get_method() == "DELETE"
    assert request.full_url == "http://core/internal/todos/todo-1"
