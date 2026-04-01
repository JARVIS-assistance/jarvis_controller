from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from jarvis_contracts.models import ErrorResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .gateway_client import GatewayClient


OPEN_PATH_PREFIXES = (
    "/health",
    "/auth/login",
    "/docs",
    "/redoc",
    "/openapi.json",
)


class GatewayAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, gateway_client: GatewayClient):  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.gateway_client = gateway_client

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS" or request.url.path.startswith(OPEN_PATH_PREFIXES):
            return await call_next(request)

        authorization = request.headers.get("authorization")
        if not authorization:
            return self._reject(request, "missing authorization header")

        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return self._reject(request, "invalid authorization header")

        try:
            principal = self.gateway_client.validate_token(
                parts[1],
                client_id=request.headers.get("x-client-id"),
                request_id=request.headers.get("x-request-id"),
            )
            request.state.principal = principal
        except HTTPException as exc:
            return self._reject(request, str(exc.detail), status_code=exc.status_code)
        except Exception as exc:
            detail = getattr(exc, "detail", "invalid or expired token")
            return self._reject(request, str(detail))

        return await call_next(request)

    @staticmethod
    def _reject(request: Request, message: str, status_code: int = 401) -> JSONResponse:
        err = ErrorResponse(
            error_code="AUTH_REQUIRED",
            message=message,
            request_id=request.headers.get("x-request-id"),
        )
        return JSONResponse(status_code=status_code, content=err.model_dump())
