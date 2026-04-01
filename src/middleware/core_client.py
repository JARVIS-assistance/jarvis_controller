from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from jarvis_contracts import InternalConversationResponse


@dataclass(slots=True)
class CoreResponse:
    mode: str
    summary: str
    content: str
    next_actions: list[str] = field(default_factory=list)


class CoreClient:
    def __init__(self, base_url: str | None = None, timeout_seconds: float = 10.0) -> None:
        self.base_url = (base_url or os.getenv("JARVIS_CORE_URL", "http://localhost:8000")).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def run_realtime_conversation(self, message: str) -> CoreResponse:
        return self._request_conversation(mode="realtime", message=message)

    def run_deep_thinking(self, message: str) -> CoreResponse:
        return self._request_conversation(mode="deep", message=message)

    def _request_conversation(self, *, mode: str, message: str) -> CoreResponse:
        payload = InternalConversationResponse.model_validate(
            self._request_json(
            "POST",
            "/internal/conversation/respond",
            body={"mode": mode, "message": message},
            )
        )
        return CoreResponse(
            mode=payload.mode,
            summary=payload.summary,
            content=payload.content,
            next_actions=list(payload.next_actions),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None,
    ) -> dict[str, object]:
        raw_body: bytes | None = None
        headers = {"accept": "application/json"}
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"

        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=raw_body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = self._decode_error_payload(exc)
            raise HTTPException(status_code=exc.code, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="core unavailable",
            ) from exc

        return json.loads(payload.decode("utf-8")) if payload else {}

    @staticmethod
    def _decode_error_payload(exc: urllib.error.HTTPError) -> str:
        payload = exc.read()
        if not payload:
            return "core request failed"
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return "core request failed"
        return str(parsed.get("message") or parsed.get("detail") or "core request failed")
