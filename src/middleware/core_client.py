from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from jarvis_contracts import InternalConversationResponse, JarvisCoreEndpoints


@dataclass(slots=True)
class CoreResponse:
    mode: str
    summary: str
    content: str
    next_actions: list[str] = field(default_factory=list)


class CoreClient:
    def __init__(
        self, base_url: str | None = None, timeout_seconds: float = 10.0
    ) -> None:
        self.base_url = (
            base_url or os.getenv("JARVIS_CORE_URL", "http://localhost:3010")
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds

    # ── conversation (기존) ─────────────────────────────────

    def run_realtime_conversation(self, message: str) -> CoreResponse:
        return self._request_conversation(mode="realtime", message=message)

    def run_deep_thinking(self, message: str) -> CoreResponse:
        return self._request_conversation(mode="deep", message=message)

    def _request_conversation(self, *, mode: str, message: str) -> CoreResponse:
        payload = InternalConversationResponse.model_validate(
            self._request_json(
                JarvisCoreEndpoints.INTERNAL_CONVERSATION_RESPOND.method,
                JarvisCoreEndpoints.INTERNAL_CONVERSATION_RESPOND.path,
                body={"mode": mode, "message": message},
            )
        )
        return CoreResponse(
            mode=payload.mode,
            summary=payload.summary,
            content=payload.content,
            next_actions=list(payload.next_actions),
        )

    # ── chat ────────────────────────────────────────────────

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
        return self._request_json(
            "POST",
            JarvisCoreEndpoints.INTERNAL_CHAT_REQUEST.path,
            body={
                "message": message,
                "task_type": task_type,
                "confirm": confirm,
                "route_override": route_override,
            },
            extra_headers={
                "x-user-id": user_id,
                "x-user-email": user_email,
                "x-request-id": request_id,
            },
        )

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
    ) -> Generator[bytes, None, None]:
        """SSE 스트리밍을 프록시로 전달하기 위한 raw byte generator."""
        raw_body = json.dumps(
            {
                "message": message,
                "task_type": task_type,
                "confirm": confirm,
                "route_override": route_override,
            }
        ).encode("utf-8")
        headers = {
            "accept": "text/event-stream",
            "content-type": "application/json",
            "x-user-id": user_id,
            "x-user-email": user_email,
            "x-request-id": request_id,
        }
        request = urllib.request.Request(
            url=f"{self.base_url}{JarvisCoreEndpoints.INTERNAL_CHAT_STREAM.path}",
            data=raw_body,
            headers=headers,
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=120)
            while True:
                line = response.readline()
                if not line:
                    break
                yield line
            response.close()
        except urllib.error.HTTPError as exc:
            detail = self._decode_error_payload(exc)
            raise HTTPException(status_code=exc.code, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="core unavailable",
            ) from exc

    # ── model config ────────────────────────────────────────

    def create_model_config(self, *, user_id: str, body: dict[str, object]) -> dict[str, object]:
        return self._request_json(
            "POST",
            JarvisCoreEndpoints.INTERNAL_CHAT_MODEL_CONFIG.path,
            body=body,
            extra_headers={"x-user-id": user_id},
        )

    def list_model_configs(self, *, user_id: str) -> list[dict[str, object]]:
        result = self._request_json(
            "GET",
            JarvisCoreEndpoints.INTERNAL_CHAT_MODEL_CONFIG_LIST.path,
            body=None,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, list) else []

    def update_model_config(
        self,
        *,
        user_id: str,
        model_config_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        return self._request_json(
            "PUT",
            JarvisCoreEndpoints.INTERNAL_CHAT_MODEL_CONFIG_UPDATE.path.format(
                model_config_id=model_config_id
            ),
            body=body,
            extra_headers={"x-user-id": user_id},
        )

    def set_model_selection(self, *, user_id: str, body: dict[str, object]) -> dict[str, object]:
        return self._request_json(
            "POST",
            JarvisCoreEndpoints.INTERNAL_CHAT_MODEL_SELECTION.path,
            body=body,
            extra_headers={"x-user-id": user_id},
        )

    def get_model_selection(self, *, user_id: str) -> dict[str, object]:
        return self._request_json(
            "GET",
            JarvisCoreEndpoints.INTERNAL_CHAT_MODEL_SELECTION_GET.path,
            body=None,
            extra_headers={"x-user-id": user_id},
        )

    def create_persona(self, *, user_id: str, body: dict[str, object]) -> dict[str, object]:
        return self._request_json(
            "POST",
            JarvisCoreEndpoints.INTERNAL_CHAT_PERSONA.path,
            body=body,
            extra_headers={"x-user-id": user_id},
        )

    def list_personas(self, *, user_id: str) -> list[dict[str, object]]:
        result = self._request_json(
            "GET",
            JarvisCoreEndpoints.INTERNAL_CHAT_PERSONA_LIST.path,
            body=None,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, list) else []

    def update_persona(
        self, *, user_id: str, user_persona_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        return self._request_json(
            "PUT",
            JarvisCoreEndpoints.INTERNAL_CHAT_PERSONA_UPDATE.path.format(
                user_persona_id=user_persona_id
            ),
            body=body,
            extra_headers={"x-user-id": user_id},
        )

    def select_persona(self, *, user_id: str, body: dict[str, object]) -> dict[str, object]:
        return self._request_json(
            "POST",
            JarvisCoreEndpoints.INTERNAL_CHAT_PERSONA_SELECT.path,
            body=body,
            extra_headers={"x-user-id": user_id},
        )

    def create_memory(self, *, user_id: str, body: dict[str, object]) -> dict[str, object]:
        return self._request_json(
            "POST",
            JarvisCoreEndpoints.INTERNAL_CHAT_MEMORY.path,
            body=body,
            extra_headers={"x-user-id": user_id},
        )

    def list_memory(self, *, user_id: str, chat_id: str | None = None) -> list[dict[str, object]]:
        path = JarvisCoreEndpoints.INTERNAL_CHAT_MEMORY_LIST.path
        if chat_id:
            path = f"{path}?chat_id={chat_id}"
        result = self._request_json(
            "GET",
            path,
            body=None,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, list) else []

    # ── common HTTP ─────────────────────────────────────────

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | list | None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object] | list:
        raw_body: bytes | None = None
        headers = {"accept": "application/json"}
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=raw_body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
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
        return str(
            parsed.get("message") or parsed.get("detail") or "core request failed"
        )
