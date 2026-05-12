from __future__ import annotations

from jarvis_controller.middleware.core_client import CoreClient


class _LineResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)
        self.closed = False

    def readline(self) -> bytes:
        return next(self._lines, b"")

    def close(self) -> None:
        self.closed = True


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
