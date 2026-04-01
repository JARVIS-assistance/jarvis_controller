from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from fastapi import HTTPException, status


@dataclass(slots=True)
class GatewayPrincipal:
    user_id: str
    tenant_id: str
    role: str
    active: bool = True


class GatewayClient:
    def __init__(self, base_url: str | None = None, timeout_seconds: float = 5.0) -> None:
        self.base_url = (base_url or os.getenv("JARVIS_GATEWAY_URL", "http://localhost:8002")).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def login(
        self,
        username: str,
        password: str,
        *,
        client_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self._request_json(
            "POST",
            "/auth/login",
            body={"username": username, "password": password},
            token=None,
            client_id=client_id,
            request_id=request_id,
        )

    def logout(
        self,
        token: str,
        *,
        client_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self._request_json(
            "POST",
            "/auth/logout",
            body=None,
            token=token,
            client_id=client_id,
            request_id=request_id,
        )

    def validate_token(
        self,
        token: str,
        *,
        client_id: str | None = None,
        request_id: str | None = None,
    ) -> GatewayPrincipal:
        payload = self._request_json(
            "GET",
            "/auth/validate",
            body=None,
            token=token,
            client_id=client_id,
            request_id=request_id,
        )
        return GatewayPrincipal(
            user_id=str(payload["user_id"]),
            tenant_id=str(payload["tenant_id"]),
            role=str(payload["role"]),
            active=bool(payload.get("active", True)),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None,
        token: str | None,
        client_id: str | None,
        request_id: str | None,
    ) -> dict[str, object]:
        raw_body: bytes | None = None
        headers = {"accept": "application/json"}
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        if token:
            headers["authorization"] = f"Bearer {token}"
        if client_id:
            headers["x-client-id"] = client_id
        if request_id:
            headers["x-request-id"] = request_id

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
                detail="gateway unavailable",
            ) from exc

        return json.loads(payload.decode("utf-8")) if payload else {}

    @staticmethod
    def _decode_error_payload(exc: urllib.error.HTTPError) -> str:
        payload = exc.read()
        if not payload:
            return "gateway request failed"
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return "gateway request failed"
        return str(parsed.get("message") or parsed.get("detail") or "gateway request failed")
