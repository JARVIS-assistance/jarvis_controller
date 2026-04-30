import logging
from pathlib import Path

from fastapi import FastAPI

from middleware.core_client import CoreClient
from middleware.auth_middleware import GatewayAuthMiddleware
from middleware.gateway_client import GatewayClient
from planner.action_context import ActionContextStore
from planner.action_dispatcher import ActionDispatcher
from router.router import api_router

logging.basicConfig(level=logging.INFO)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    repo_root = Path(__file__).resolve().parents[2]
    for env_path in (repo_root / ".env", repo_root / "jarvis_controller" / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


API_DESCRIPTION = """
JARVIS controller public API.

This service is the orchestration layer in front of gateway/core services and exposes
conversation, auth, execution, and model-configuration endpoints.

Role groups:
- `health`: service availability and liveness checks
- `auth`: login, signup, logout, current user identity
- `conversation`: orchestration entrypoint for planning/realtime/deep response flows
- `chat`: direct chat request/stream and model configuration delegation
- `execution`: action execution and verification endpoints

Authentication:
- Public endpoints: `/health`, `/auth/login`, `/docs`, `/redoc`, `/openapi.json`
- Public signup endpoint: `/auth/signup`
- Protected auth endpoints: `/auth/logout`, `/auth/me`
- Protected endpoints: send `Authorization: Bearer <token>`
- Optional tracing header: `x-request-id`
- Optional client header: `x-client-id`
""".strip()

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "서비스 상태 확인용 엔드포인트.",
    },
    {
        "name": "auth",
        "description": "로그인, 회원가입, 로그아웃, 현재 사용자 조회를 담당한다.",
    },
    {
        "name": "conversation",
        "description": "planning / realtime / deep 흐름을 오케스트레이션하는 진입점이다.",
    },
    {
        "name": "chat",
        "description": "직접 채팅 요청과 모델 설정 위임을 담당한다.",
    },
    {
        "name": "execution",
        "description": "액션 실행과 검증을 담당한다.",
    },
]


def create_app(
    gateway_client: GatewayClient | None = None,
    core_client: CoreClient | None = None,
) -> FastAPI:
    app = FastAPI(
        title="JARVIS Controller API",
        description=API_DESCRIPTION,
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.gateway_client = gateway_client or GatewayClient()
    app.state.core_client = core_client or CoreClient()
    app.state.action_dispatcher = ActionDispatcher()
    app.state.action_context = ActionContextStore()
    app.state.action_dispatcher.context_store = app.state.action_context
    app.add_middleware(GatewayAuthMiddleware, gateway_client=app.state.gateway_client)
    app.include_router(api_router)
    return app


app = create_app()
