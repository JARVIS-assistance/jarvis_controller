from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from jarvis_contracts import (
    DeepThinkPlanResponse,
    DeepThinkResponse,
    InternalConversationResponse,
    JarvisCoreEndpoints,
)


@dataclass(slots=True)
class CoreResponse:
    mode: str
    summary: str
    content: str
    next_actions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CoreBinaryResponse:
    content: bytes
    media_type: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CoreStreamResponse:
    body: Generator[bytes, None, None]
    media_type: str
    headers: dict[str, str] = field(default_factory=dict)


class CoreClient:
    def __init__(
        self, base_url: str | None = None, timeout_seconds: float = 10.0
    ) -> None:
        self.base_url = (
            base_url or os.getenv("JARVIS_CORE_URL", "http://localhost:3010")
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.deepthink_timeout_seconds = float(
            os.getenv("JARVIS_DEEPTHINK_TIMEOUT_SECONDS", "120")
        )

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
            event_buffer: list[bytes] = []
            while True:
                line = response.readline()
                if not line:
                    if event_buffer:
                        yield b"".join(event_buffer)
                    break
                event_buffer.append(line)
                if line in {b"\n", b"\r\n"}:
                    yield b"".join(event_buffer)
                    event_buffer = []
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

    def delete_model_config(
        self,
        *,
        user_id: str,
        model_config_id: str,
    ) -> dict[str, object]:
        return self._request_json(
            "DELETE",
            JarvisCoreEndpoints.INTERNAL_CHAT_MODEL_CONFIG_DELETE.path.format(
                model_config_id=model_config_id
            ),
            body=None,
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

    # ── audio ───────────────────────────────────────────────

    def synthesize_speech(
        self,
        *,
        user_id: str,
        body: dict[str, object],
        request_id: str = "",
    ) -> CoreBinaryResponse:
        raw_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self.base_url}{JarvisCoreEndpoints.INTERNAL_AUDIO_SPEECH.path}",
            data=raw_body,
            headers={
                "accept": "*/*",
                "content-type": "application/json",
                "x-user-id": user_id,
                "x-request-id": request_id,
            },
            method=JarvisCoreEndpoints.INTERNAL_AUDIO_SPEECH.method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content = response.read()
                media_type = response.headers.get("content-type", "audio/mpeg").split(
                    ";", 1
                )[0]
                headers = {
                    name: response.headers[name]
                    for name in (
                        "x-tts-provider",
                        "x-tts-model",
                        "x-tts-voice",
                        "x-tts-format",
                        "x-ai-generated-voice",
                    )
                    if name in response.headers
                }
                return CoreBinaryResponse(
                    content=content,
                    media_type=media_type,
                    headers=headers,
                )
        except urllib.error.HTTPError as exc:
            detail = self._decode_error_payload(exc)
            raise HTTPException(status_code=exc.code, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="core unavailable",
            ) from exc

    def synthesize_speech_pcm_stream(
        self,
        *,
        user_id: str,
        body: dict[str, object],
        request_id: str = "",
    ) -> CoreStreamResponse:
        raw_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self.base_url}{JarvisCoreEndpoints.INTERNAL_AUDIO_SPEECH_PCM.path}",
            data=raw_body,
            headers={
                "accept": "audio/pcm",
                "content-type": "application/json",
                "x-user-id": user_id,
                "x-request-id": request_id,
            },
            method=JarvisCoreEndpoints.INTERNAL_AUDIO_SPEECH_PCM.method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            detail = self._decode_error_payload(exc)
            raise HTTPException(status_code=exc.code, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="core unavailable",
            ) from exc

        media_type = response.headers.get("content-type", "audio/pcm").split(";", 1)[0]
        headers = {
            name: response.headers[name]
            for name in (
                "x-tts-provider",
                "x-tts-model",
                "x-tts-voice",
                "x-tts-format",
                "x-tts-sample-rate",
                "x-tts-channels",
                "x-tts-sample-width",
                "x-tts-chunk-count",
                "x-ai-generated-voice",
            )
            if name in response.headers
        }

        def stream_body() -> Generator[bytes, None, None]:
            try:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                response.close()

        return CoreStreamResponse(
            body=stream_body(),
            media_type=media_type,
            headers=headers,
        )

    def list_speech_models(
        self,
        *,
        user_id: str,
        request_id: str = "",
    ) -> dict[str, object]:
        request = urllib.request.Request(
            url=f"{self.base_url}{JarvisCoreEndpoints.INTERNAL_AUDIO_SPEECH_MODELS.path}",
            headers={
                "accept": "application/json",
                "x-user-id": user_id,
                "x-request-id": request_id,
            },
            method=JarvisCoreEndpoints.INTERNAL_AUDIO_SPEECH_MODELS.method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = self._decode_error_payload(exc)
            raise HTTPException(status_code=exc.code, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="core unavailable",
            ) from exc

    def set_runtime_profile(
        self, *, user_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        result = self._request_json(
            JarvisCoreEndpoints.INTERNAL_CLIENT_RUNTIME_PROFILE.method,
            JarvisCoreEndpoints.INTERNAL_CLIENT_RUNTIME_PROFILE.path,
            body=body,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, dict) else {}

    def get_runtime_profile(self, *, user_id: str) -> dict[str, object]:
        result = self._request_json(
            JarvisCoreEndpoints.INTERNAL_CLIENT_RUNTIME_PROFILE_GET.method,
            JarvisCoreEndpoints.INTERNAL_CLIENT_RUNTIME_PROFILE_GET.path,
            body=None,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, dict) else {}

    # ── todos ──────────────────────────────────────────────

    def create_todo(self, *, user_id: str, body: dict[str, object]) -> dict[str, object]:
        result = self._request_json(
            JarvisCoreEndpoints.INTERNAL_TODOS.method,
            JarvisCoreEndpoints.INTERNAL_TODOS.path,
            body=body,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, dict) else {}

    def list_todos(
        self,
        *,
        user_id: str,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> dict[str, object]:
        query = urllib.parse.urlencode(
            {
                "include_deleted": str(include_deleted).lower(),
                "limit": limit,
                **({"status": status} if status else {}),
            }
        )
        result = self._request_json(
            JarvisCoreEndpoints.INTERNAL_TODOS_LIST.method,
            f"{JarvisCoreEndpoints.INTERNAL_TODOS_LIST.path}?{query}",
            body=None,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, dict) else {"items": []}

    def get_todo(self, *, user_id: str, todo_id: str) -> dict[str, object]:
        result = self._request_json(
            JarvisCoreEndpoints.INTERNAL_TODO_DETAIL.method,
            JarvisCoreEndpoints.INTERNAL_TODO_DETAIL.path.format(todo_id=todo_id),
            body=None,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, dict) else {}

    def update_todo(
        self,
        *,
        user_id: str,
        todo_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        result = self._request_json(
            JarvisCoreEndpoints.INTERNAL_TODO_UPDATE.method,
            JarvisCoreEndpoints.INTERNAL_TODO_UPDATE.path.format(todo_id=todo_id),
            body=body,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, dict) else {}

    def delete_todo(self, *, user_id: str, todo_id: str) -> dict[str, object]:
        result = self._request_json(
            JarvisCoreEndpoints.INTERNAL_TODO_DELETE.method,
            JarvisCoreEndpoints.INTERNAL_TODO_DELETE.path.format(todo_id=todo_id),
            body=None,
            extra_headers={"x-user-id": user_id},
        )
        return result if isinstance(result, dict) else {}

    # ── deepthink ───────────────────────────────────────────

    def deepthink_plan(
        self,
        *,
        request_id: str,
        message: str,
        user_id: str,
    ) -> DeepThinkPlanResponse:
        raw = self._request_json(
            JarvisCoreEndpoints.INTERNAL_DEEPTHINK_PLAN.method,
            JarvisCoreEndpoints.INTERNAL_DEEPTHINK_PLAN.path,
            body={"request_id": request_id, "message": message},
            extra_headers={"x-user-id": user_id, "x-request-id": request_id},
            timeout_seconds=self.deepthink_timeout_seconds,
        )
        return DeepThinkPlanResponse.model_validate(raw)

    def deepthink_execute(
        self,
        *,
        request_id: str,
        message: str,
        plan_steps: list[dict[str, str]],
        user_id: str,
        execution_context: list[str] | None = None,
    ) -> DeepThinkResponse:
        raw = self._request_json(
            JarvisCoreEndpoints.INTERNAL_DEEPTHINK_EXECUTE.method,
            JarvisCoreEndpoints.INTERNAL_DEEPTHINK_EXECUTE.path,
            body={
                "request_id": request_id,
                "message": message,
                "plan_steps": plan_steps,
                "execution_context": execution_context or [],
            },
            extra_headers={"x-user-id": user_id, "x-request-id": request_id},
            timeout_seconds=self.deepthink_timeout_seconds,
        )
        return DeepThinkResponse.model_validate(raw)

    # ── common HTTP ─────────────────────────────────────────

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | list | None,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
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
                request, timeout=timeout_seconds or self.timeout_seconds
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
